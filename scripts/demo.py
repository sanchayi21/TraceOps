"""Run a TraceOps demonstration scenario end to end.

    python scripts/demo.py healthy        # traffic only; nothing should alert
    python scripts/demo.py db-down        # inventory loses its database
    python scripts/demo.py slow-db        # inventory's queries take 1.5 s
    python scripts/demo.py timeout        # queries take 5 s -> orders times out
    python scripts/demo.py inventory-down # the inventory container is stopped

Each scenario: healthy baseline traffic -> inject fault -> wait for
Prometheus/Alertmanager to open an incident -> wait for the investigation ->
print the findings -> remove the fault.

Requires the stack to be running (`docker compose up -d --build`).
Uses only httpx, which is already in backend/requirements.txt.
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx

API = "http://127.0.0.1:8000"
BASELINE_SECONDS = 30
COOLDOWN_SECONDS = 180
ALERT_TIMEOUT_SECONDS = 180

SCENARIOS = {
    "healthy": None,
    "db-down": {"mode": "db_down"},
    "slow-db": {"mode": "slow_db", "latency_ms": 1500},
    "timeout": {"mode": "slow_db", "latency_ms": 5000},
    "inventory-down": "stop-container",
}


def step(message: str):
    print(f"\n[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def api(method: str, path: str, **kwargs):
    response = httpx.request(method, f"{API}{path}", timeout=15, **kwargs)
    response.raise_for_status()
    return response.json()


def compose(*args: str):
    subprocess.run(["docker", "compose", *args], check=True)


def open_incidents():
    return [i for i in api("GET", "/api/incidents") if i["status"] == "open"]


def wait_for_quiet():
    """Wait until earlier incidents resolve, then a cooldown, so this run's
    'before' (baseline) metrics aren't polluted by the previous scenario."""
    if open_incidents():
        step("An earlier incident is still open; waiting for its alerts to resolve...")
        deadline = time.time() + 240
        while open_incidents() and time.time() < deadline:
            time.sleep(5)
        if open_incidents():
            step("Still open after 4 min; resolving it manually so this run starts clean.")
            for incident in open_incidents():
                api("POST", f"/api/incidents/{incident['id']}/resolve")

    resolved = [i["resolved_at"] for i in api("GET", "/api/incidents") if i["resolved_at"]]
    if resolved:
        last = datetime.fromisoformat(max(resolved).replace("Z", "+00:00"))
        remaining = COOLDOWN_SECONDS - (datetime.now(timezone.utc) - last).total_seconds()
        if remaining > 0:
            step(f"Cooling down {remaining:.0f}s after the previous incident...")
            time.sleep(remaining)


def wait_for_incident(known_ids: set[int]):
    deadline = time.time() + ALERT_TIMEOUT_SECONDS
    while time.time() < deadline:
        new = [i for i in api("GET", "/api/incidents") if i["id"] not in known_ids]
        if new and new[0]["investigation_status"] in ("completed", "failed"):
            return new[0]
        time.sleep(5)
    return None


def print_report(incident_id: int):
    incident = api("GET", f"/api/incidents/{incident_id}")
    report = incident["report"]
    print(f"\n=== Incident #{incident['id']}: {incident['title']}")
    print(f"    alerts: {', '.join(a['alertname'] + '/' + str(a['service']) for a in incident['alerts'])}")
    if not report:
        print(f"    investigation {incident['investigation_status']}: {incident['error']}")
        return
    print(f"\nSUMMARY\n  {report['summary']}")
    print("\nOBSERVED (symptoms)")
    for symptom in report["symptoms"]:
        print(f"  - {symptom['description']}  {symptom['evidence_ids']}")
    print("\nINFERRED (likely failure points)")
    for h in report["hypotheses"]:
        print(f"  {h['rank']}. {h['component']} [{h['failure_type']}] "
              f"{h['confidence_label']} ({h['confidence']})  {h['evidence_ids']}")
    print("\nPROPAGATION (representative trace "
          f"{report['representative_trace_id']})")
    for hop in report["propagation"]:
        marker = "  <-- origin" if hop["is_origin"] else ""
        print(f"  {hop['service']:>9} {hop['kind']:<6} {hop['operation']:<32} "
              f"{hop['status']:<5} {hop['duration_ms']:>8.1f} ms {hop['error'] or ''}{marker}")
    print("\nUNCERTAINTY")
    for note in report["uncertainty"]:
        print(f"  - {note}")
    print(f"\nOpen the dashboard: http://localhost:5173/#/incidents/{incident['id']}")


def run(name: str):
    fault = SCENARIOS[name]
    api("GET", "/api/demo/status")  # fails fast if TraceOps is not running
    wait_for_quiet()
    known = {i["id"] for i in api("GET", "/api/incidents")}

    step(f"Starting traffic: 4 requests/s through the gateway")
    api("POST", "/api/demo/traffic",
        json={"duration_seconds": 240, "requests_per_second": 4})
    step(f"Healthy baseline for {BASELINE_SECONDS}s...")
    time.sleep(BASELINE_SECONDS)

    try:
        if fault is None:
            step("No fault injected. Waiting 60s to confirm nothing alerts...")
            time.sleep(60)
            new = [i for i in api("GET", "/api/incidents") if i["id"] not in known]
            print("  OK: no incident opened." if not new else f"  Unexpected: {new}")
            return
        if fault == "stop-container":
            step("Stopping the inventory container (docker compose stop inventory)")
            compose("stop", "inventory")
        else:
            step(f"Injecting fault into inventory: {fault}")
            api("PUT", "/api/demo/fault", json=fault)

        step("Waiting for an alert -> incident -> investigation (usually 30-90s)...")
        incident = wait_for_incident(known)
        if incident is None:
            print("  No incident within the time limit. Check http://localhost:9090/alerts")
            return
        # Related alerts keep arriving for a little while; give the
        # investigation a moment to include them.
        time.sleep(20)
        while api("GET", f"/api/incidents/{incident['id']}")["investigation_status"] in (
            "pending", "running"):
            time.sleep(3)
        print_report(incident["id"])
    finally:
        step("Cleaning up: removing the fault and stopping traffic")
        api("DELETE", "/api/demo/traffic")
        if fault == "stop-container":
            compose("start", "inventory")
        elif fault:
            api("DELETE", "/api/demo/fault")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", choices=SCENARIOS)
    args = parser.parse_args()
    try:
        run(args.scenario)
    except httpx.HTTPError as error:
        sys.exit(f"Cannot talk to TraceOps at {API} ({error}). "
                 "Is the stack running? docker compose up -d --build")
