# TraceOps

TraceOps is an incident investigator for distributed applications. When an alert fires, it
pulls **metrics** (Prometheus), **traces** (Tempo) and **logs** (Loki) for the affected time
window. It finds where the failure *started*, as opposed to where it was *noticed*, and records
a structured incident with every claim linked to the evidence behind it.

It ships with a small three-service demo application and a fault injector, so you can break
things on purpose and watch TraceOps work out what happened.

```
                ┌──────────── demo application ────────────┐
  traffic ──►  Gateway :8003 ──► Orders :8002 ──► Inventory :8001 ──► PostgreSQL :5432
                   │                 │                  │
                   └── OpenTelemetry traces + logs (OTLP) ──► Tempo :3200 / Loki :3100
                   └── /metrics scraped every 5 s ─────────► Prometheus :9090
                                                                 │ alert rules
                                                                 ▼
                                                           Alertmanager :9093
                                                                 │ webhook
                                                                 ▼
  Dashboard :5173 (React) ◄── REST ── TraceOps API :8000 ── queries Prometheus, Tempo, Loki
                                          │
                                          └── incidents ──► TraceOps DB (PostgreSQL :5433)
```

## What it does (implemented and verified)

1. **Instrumented services.** Every request gets a W3C `traceparent` header, so one request has
   *one* trace ID across gateway → orders → inventory → PostgreSQL (database calls have their
   own spans). Logs are JSON and carry the same `trace_id`. Prometheus metrics record request
   counts and latency per route and status.
2. **Alerting.** Prometheus rules (`HighErrorRate`, `HighLatency`, `ServiceDown`) send alerts
   to Alertmanager, which sends them to TraceOps' webhook.
3. **Incident correlation.** One failure trips several alerts. Alerts arriving while an
   incident is open are merged into it, and a new firing alert re-runs the investigation.
   When every alert has resolved, the incident is marked resolved.
4. **Investigation** ([backend/traceops/investigator.py](backend/traceops/investigator.py)):
   - *Symptoms* (observed): per-service 5xx ratio, p95 latency and scrape health, each compared
     with the previous window of the same length.
   - *Error origin* (inferred): in each failed trace, the error spans form a chain from the
     database up to the gateway. The **deepest error span with no failing children** is where
     the failure started. Traces are grouped by origin.
   - *Bottlenecks*: for slow or timed-out requests, the span with the largest **self time**
     (its duration minus its children's) is where the time went.
   - *Logs*: error logs from the implicated service, plus every log line that shares the
     representative trace's `trace_id`.
   - *Confidence*: `0.6 × share of failed traces explained + 0.2 × logs agree + 0.2 × metrics
     agree`. This is a transparent heuristic, not a probability, and the report says so.
   - *Uncertainty*: missing data sources, competing failure points, and caveats specific to
     the failure type.
5. **Dashboard** (React and TypeScript), written for people who aren't observability experts:
   - a live map of the demo shop (Gateway → Orders → Inventory → Database) showing what's working;
   - a guided 3-step demo: send traffic, break something, watch the incident appear;
   - each incident told as a story: the most likely cause in one sentence, what users
     experienced, a map showing where it started (red) and what it affected (orange), one real
     failed request step by step, why TraceOps thinks so (traces, logs, metrics as a checklist),
     what it's unsure about, and what to check next;
   - the raw evidence, exact queries and Grafana links, folded under "Technical details";
   - a "How it works" page with a glossary.
6. **Optional AI explanation.** A local Ollama model can turn the report into prose that cites
   evidence IDs. It's off by default; the deterministic summary needs no LLM.

## Quick start

Prerequisites: Docker Desktop and Node.js 18+ (Python 3.10+ is only needed for the tests and
the demo script).

```bash
cp .env.example .env            # then edit the password (skip if you already have a .env)
docker compose up -d --build    # 11 containers; first build takes a few minutes
cd frontend && npm install && npm run dev
```

Open **http://localhost:5173**.

| URL | What |
|---|---|
| http://localhost:5173 | TraceOps dashboard |
| http://localhost:8000/docs | TraceOps API (Swagger UI) |
| http://localhost:8003/orders/check/101 | Demo app via the gateway |
| http://localhost:3000 | Grafana: raw metrics, traces, logs |
| http://localhost:9090/alerts | Prometheus alert state |

Stop with `docker compose down`. Data volumes are kept; add `-v` to delete them.

## Running a demonstration

From the dashboard: **Start traffic**, wait about 30 s, click a fault, then watch an incident
appear (usually 30–60 s later). Click **Clear fault** afterwards.

Or run a scripted scenario that does the same and prints the findings:

```bash
backend/.venv/Scripts/python scripts/demo.py db-down     # Windows venv path
python scripts/demo.py slow-db                           # any Python with httpx installed
```

| Scenario | What is broken | What TraceOps should conclude |
|---|---|---|
| `db-down` | inventory's DB connections fail | **postgresql** unavailable (from inventory); the error travels inventory 503 → orders 502 → gateway 502 |
| `slow-db` | every DB query takes 1.5 s (real `pg_sleep`) | slow dependency: the `SELECT pg_sleep` span in PostgreSQL |
| `timeout` | DB queries take 5 s, so orders times out after 4 s | slow dependency in PostgreSQL, *not* orders, even though orders is where the timeout error appears |
| `inventory-down` | `docker compose stop inventory` | **inventory** unreachable, corroborated by Prometheus's `ServiceDown` |
| `healthy` | nothing | no incident |

Faults are injected through the inventory service's `/chaos` endpoint, which exists only
when `CHAOS_ENABLED=true`. TraceOps is never told which fault is active; it works only from
the telemetry.

## Tests

```bash
# backend: trace analysis, investigator, alert correlation, API (no Docker needed)
cd backend && .venv/Scripts/python -m pytest -q
# demo services: status mapping, fault injection, metrics
backend/.venv/Scripts/python -m pytest -q services/tests
```

The investigator tests use synthetic traces in the same JSON shape Tempo returns, so the
correlation logic is tested without any infrastructure. To set up the venv from scratch:
`python -m venv backend/.venv`, then `pip install -r backend/requirements.txt -r
services/requirements.txt`.

## Project layout

```
backend/
  main.py                    API routes, webhook, background investigations
  traceops/
    trace_analysis.py        pure logic: parse traces, error origins, self time
    investigator.py          the workflow: metrics → traces → logs → scored report
    telemetry_clients.py     Prometheus / Tempo / Loki HTTP clients
    alerts.py                Alertmanager payload → incident (merge, resolve)
    store.py                 PostgreSQL storage (report kept as JSONB)
    models.py                report/incident schema (symptoms vs hypotheses vs evidence)
    explain.py               deterministic summary + optional Ollama explanation
    demo.py                  demo controls (fault injection, traffic generator)
  tests/
services/
  common/telemetry.py        OpenTelemetry tracing + JSON logs + Prometheus metrics
  common/chaos.py            fault injection state and /chaos endpoints
  gateway/ orders/ inventory/
  tests/
infra/                       Prometheus rules, Alertmanager, Tempo, Loki, Grafana config
frontend/                    React + TypeScript dashboard (Vite)
scripts/demo.py              scripted demonstration scenarios
```

## Design decisions

- **TraceOps has its own database** (`traceops-db`, port 5433). If it shared the demo
  app's PostgreSQL, the `db-down` incident would stop TraceOps from recording it.
- **Timeouts nest inward:** DB connect 2 s < orders→inventory 4 s < gateway→orders 6 s. When
  the inner timeout is shorter, the service closest to the problem reports it first.
- **Pull, don't push, for investigation.** Services only emit standard telemetry; TraceOps
  queries the telemetry backends afterwards, so investigating needs no changes to the services.
- **Reports are JSONB, validated by Pydantic.** The report shape changes as the investigator
  improves; the fields used for listing and filtering are real columns.
- **No Celery/Redis.** An investigation takes a few seconds, so FastAPI `BackgroundTasks`
  is enough. A queue would only be justified by long or very frequent investigations.

## Real problems found while building it

These came up while running the scenarios, and each one is now fixed and covered by a test:

1. **Misattributed outage.** The original code had a 3 s timeout both for inventory's DB
   connection and for orders' call to inventory. With the database down, orders gave up first
   and reported *"Cannot reach Inventory Service"*, blaming the wrong component. The fix was to
   nest the timeouts.
2. **Tempo drops leading zeros from trace IDs** in search results (31 characters instead of 32),
   so joining traces to logs by `trace_id` silently failed for about 1 in 16 traces. TraceOps
   now pads IDs to 32 characters.
3. **`ConnectTimeout` is a subclass of `TimeoutException` in httpx**, so a stopped service was
   reported as "timed out" (504) instead of "unreachable" (503). TraceOps classifies by the
   span's exception type; the services now catch connect errors first.
4. **Thread-pool starvation made healthy services look down.** With inventory stopped, DNS
   lookups for it hung inside Docker and filled FastAPI's thread pool. The sync `/metrics`
   handler then couldn't run, so Prometheus marked the gateway and orders as down too.
   `/metrics` is now `async`.

## Optional: AI explanations with a local model (free)

1. Install [Ollama](https://ollama.com) and run `ollama pull llama3.2`.
2. In `.env`, set `EXPLAIN_PROVIDER=ollama`, then run `docker compose up -d traceops`.
3. Click **Explain with AI** on an incident.

The model only receives the structured report and is told to use only those facts and to
cite evidence IDs. Treat its output as a readable rewording; the evidence remains the source
of truth. No paid API is used.

## Limitations

- The demo runs on one machine; storage for Tempo and Loki is in-container and resets when
  those containers are recreated.
- Investigation examines at most 40 traces per run, which is fine for a demo but would need
  sampling strategies at scale.
- Correlation is by time window plus trace structure. It doesn't yet use a service dependency
  graph, deployment events or configuration changes.
- The confidence formula is a hand-tuned heuristic, not a calibrated model.
- No authentication on the API or dashboard; everything binds to `127.0.0.1` and is meant for
  local use only.
