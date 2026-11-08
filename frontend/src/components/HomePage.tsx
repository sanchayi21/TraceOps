import { useState } from "react";
import { api } from "../api";
import { confidenceWords, displayName, timeAgo, userImpact, verdict } from "../plain";
import type { DemoStatus, IncidentSummary } from "../types";
import { usePolling } from "../usePolling";
import SystemMap, { type NodeState } from "./SystemMap";

const FAULTS = [
  {
    title: "Database goes offline",
    text: "Inventory can no longer connect to the database. Requests start failing.",
    fault: { mode: "db_down" },
  },
  {
    title: "Database gets slow",
    text: "Every query waits 1.5 s. Requests still work, just slowly.",
    fault: { mode: "slow_db", latency_ms: 1500 },
  },
  {
    title: "Database gets very slow",
    text: "Queries take 5 s, so Orders gives up waiting (a timeout).",
    fault: { mode: "slow_db", latency_ms: 5000 },
  },
  {
    title: "Inventory starts crashing",
    text: "Half of Inventory's requests fail with an internal error.",
    fault: { mode: "error", error_rate: 0.5 },
  },
];

const FAULT_NAMES: Record<string, string> = {
  db_down: "Database offline",
  slow_db: "Database slow",
  error: "Inventory crashing",
};

/** Work out what each box on the map should show from live service status. */
function liveStates(status: DemoStatus | null) {
  const states: Record<string, NodeState> = {};
  const notes: Record<string, string> = {};
  if (!status) return { states, notes };
  for (const service of status.services) {
    states[service.name] = service.healthy ? "ok" : "down";
  }
  const fault = status.services.find((s) => s.name === "inventory")?.fault;
  const inventoryUp = states.inventory === "ok";
  states.database = inventoryUp ? "ok" : "unknown";
  if (fault?.mode === "db_down") {
    states.database = "down";
    notes.database = "Broken on purpose (demo)";
  } else if (fault?.mode === "slow_db") {
    states.database = "slow";
    notes.database = `+${(fault.latency_ms / 1000).toFixed(1)} s per query (demo)`;
  } else if (fault?.mode === "error") {
    states.inventory = "slow";
    notes.inventory = `${Math.round(fault.error_rate * 100)}% of requests fail (demo)`;
  }
  return { states, notes };
}

function IncidentCard({ incident }: { incident: IncidentSummary }) {
  const working = incident.investigation_status === "pending" || incident.investigation_status === "running";
  const headline =
    incident.top_service && incident.top_failure_type
      ? verdict({ service: incident.top_service, failure_type: incident.top_failure_type })
      : working
        ? "Investigating…"
        : incident.investigation_status === "failed"
          ? "Investigation failed"
          : "No clear cause found";
  const impact = userImpact(incident.symptoms);
  const status = working ? "investigating" : incident.status;

  return (
    <a className="incident-card" href={`#/incidents/${incident.id}`}>
      <div className="incident-card-top">
        <span className={`pill pill-${status}`}>
          {status === "open" ? "Ongoing" : status === "resolved" ? "Resolved" : "Investigating"}
        </span>
        <span className="muted small">
          #{incident.id} · {timeAgo(incident.created_at)}
          {incident.source === "manual" && " · checked by hand"}
        </span>
      </div>
      <div className="incident-card-title">{headline}</div>
      <div className="incident-card-meta">
        {impact && <span>Users: {impact}</span>}
        {incident.confidence_label && (
          <span className={`conf conf-${incident.confidence_label}`}>
            {confidenceWords(incident.confidence_label)}
          </span>
        )}
        {incident.top_service && <span className="muted">Where: {displayName(incident.top_service)}</span>}
      </div>
    </a>
  );
}

export default function HomePage() {
  const { data: status, refresh: refreshStatus } = usePolling(api.demoStatus, 3000);
  const { data: incidents, error, refresh: refreshIncidents } = usePolling(api.incidents, 4000);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function act(label: string, action: () => Promise<unknown>) {
    setBusy(true);
    try {
      await action();
      setMessage(label);
    } catch (e) {
      setMessage(`Something went wrong: ${e instanceof Error ? e.message : e}`);
    }
    setBusy(false);
    refreshStatus();
    refreshIncidents();
  }

  async function checkNow() {
    const incident = await api.investigate(10);
    window.location.hash = `#/incidents/${incident.id}`;
  }

  const { states, notes } = liveStates(status);
  const fault = status?.services.find((s) => s.name === "inventory")?.fault;
  const activeFault = fault && fault.mode !== "none" ? fault : null;
  const traffic = status?.traffic;
  const okCount = traffic ? traffic.status_counts["200"] ?? 0 : 0;
  const failCount = traffic ? traffic.sent - okCount - (traffic.status_counts["404"] ?? 0) : 0;
  const ongoing = incidents?.find((i) => i.status === "open");

  return (
    <div className="home">
      <section className="hero">
        <h1>When an app breaks, TraceOps finds out where and why.</h1>
        <p>
          Modern apps are made of several small services that call each other. When one breaks,
          they <em>all</em> start showing errors, so it's hard to tell the cause from the victims.
          TraceOps collects the evidence (request recordings, error messages and measurements),
          follows failed requests step by step, and tells you where the problem started.
        </p>
        <a href="#/how-it-works">How it works →</a>
      </section>

      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>The demo shop, live</h2>
            <p className="muted small">
              A small online shop TraceOps is watching. Every user request travels left to right.
            </p>
          </div>
        </div>
        <SystemMap states={states} notes={notes} />
        {ongoing && (
          <a className="banner banner-bad" href={`#/incidents/${ongoing.id}`}>
            An incident is ongoing: open the investigation →
          </a>
        )}
      </section>

      <div className="home-grid">
        <section className="panel">
          <h2>Try it: break something on purpose</h2>
          <p className="muted small">
            TraceOps isn't told what you broke. It has to work it out from the evidence.
          </p>

          <div className="step">
            <div className="step-num">1</div>
            <div className="step-body">
              <h3>Send some customer traffic</h3>
              <p className="muted small">Simulated shoppers send 4 requests per second, so there's something to watch.</p>
              {traffic?.active ? (
                <p className="small">
                  <span className="live-dot" /> Sending… {traffic.seconds_left}s left ·{" "}
                  <span className="good">{okCount} succeeded</span>
                  {failCount > 0 && <span className="bad"> · {failCount} failed</span>}{" "}
                  <button className="link" onClick={() => act("Traffic stopped.", api.stopTraffic)}>
                    stop
                  </button>
                </p>
              ) : (
                <button disabled={busy} onClick={() => act("Traffic started for 3 minutes.", () => api.startTraffic(180))}>
                  Start traffic (3 min)
                </button>
              )}
            </div>
          </div>

          <div className="step">
            <div className="step-num">2</div>
            <div className="step-body">
              <h3>Wait 30 seconds, then choose what to break</h3>
              {!traffic?.active && <p className="muted small">Start traffic first, otherwise there's nothing to break.</p>}
              {activeFault ? (
                <div className="active-fault">
                  <span>
                    Broken right now: <strong>{FAULT_NAMES[activeFault.mode] ?? activeFault.mode}</strong>
                  </span>
                  <button disabled={busy} onClick={() => act("Fixed. The system will recover in about a minute.", api.clearFault)}>
                    Fix it
                  </button>
                </div>
              ) : (
                <div className="fault-grid">
                  {FAULTS.map((f) => (
                    <button
                      key={f.title}
                      className="fault-card"
                      disabled={busy || !traffic?.active}
                      onClick={() => act(`Broken: ${f.title}.`, () => api.injectFault(f.fault))}
                    >
                      <strong>{f.title}</strong>
                      <span>{f.text}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div className="step">
            <div className="step-num">3</div>
            <div className="step-body">
              <h3>Wait about a minute</h3>
              <p className="muted small">
                Monitoring notices the problem and raises an alarm. TraceOps then investigates and the
                incident appears in the list. Click it to see what TraceOps found. Afterwards, click{" "}
                <strong>Fix it</strong>.
              </p>
            </div>
          </div>
          {message && <p className="notice small">{message}</p>}
          <details className="small muted">
            <summary>Advanced: simulate a crashed service</summary>
            <p>
              In a terminal, run <code>docker compose stop inventory</code>. Afterwards, bring it back with{" "}
              <code>docker compose start inventory</code>.
            </p>
          </details>
        </section>

        <section className="panel">
          <div className="panel-head">
            <div>
              <h2>Incidents</h2>
              <p className="muted small">Opened automatically when monitoring raises an alarm.</p>
            </div>
            <button className="secondary" onClick={checkNow} title="Run an investigation on the last 10 minutes without waiting for an alarm">
              Check last 10 min
            </button>
          </div>
          {error && <p className="error">Can't reach TraceOps: {error}. Is <code>docker compose up -d</code> running?</p>}
          {incidents && incidents.length === 0 && (
            <p className="muted">No incidents yet. Follow the 3 steps to create one.</p>
          )}
          <div className="incident-cards">
            {incidents?.map((incident) => <IncidentCard key={incident.id} incident={incident} />)}
          </div>
        </section>
      </div>
    </div>
  );
}
