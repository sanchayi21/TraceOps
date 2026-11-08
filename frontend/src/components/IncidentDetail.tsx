import { useState } from "react";
import { api } from "../api";
import {
  confidenceWords,
  describeSymptom,
  displayName,
  failureStory,
  nextChecks,
  timeAgo,
  toNode,
  userImpact,
  verdict,
  type NodeId,
} from "../plain";
import type { Evidence, Hypothesis, Incident, PropagationHop, PublicConfig, Report } from "../types";
import { formatTime, usePolling } from "../usePolling";
import SystemMap, { type NodeState } from "./SystemMap";

// ------------------------------------------------------------------ helpers

function grafanaTraceUrl(grafana: string, traceId: string): string {
  const panes = {
    a: {
      datasource: "tempo",
      queries: [{ refId: "A", datasource: { type: "tempo", uid: "tempo" }, queryType: "traceql", query: traceId }],
      range: { from: "now-24h", to: "now" },
    },
  };
  return `${grafana}/explore?schemaVersion=1&panes=${encodeURIComponent(JSON.stringify(panes))}`;
}

function numbersFrom(text: string): [number, number] | null {
  const match = text.match(/(\d+) of (\d+)/);
  return match ? [Number(match[1]), Number(match[2])] : null;
}

interface Check {
  ok: boolean;
  title: string;
  text: string;
}

/** The three kinds of evidence behind the top hypothesis, in plain words. */
function evidenceChecks(top: Hypothesis, evidence: Evidence[]): Check[] {
  const cited = evidence.filter((e) => top.evidence_ids.includes(e.id));
  const where = displayName(top.service);

  const trace = cited.find((e) => e.kind === "trace");
  const matched = (trace?.data.matched as number | undefined) ?? (trace ? numbersFrom(trace.title)?.[0] : 0) ?? 0;
  const total = (trace?.data.total as number | undefined) ?? (trace ? numbersFrom(trace.title)?.[1] : 0) ?? 0;
  const slow = trace?.data.trace_kind === "slow" || trace?.title.includes("slow traces");

  const log = cited.find((e) => e.kind === "log" && !e.trace_id);
  const logCount = (log?.data.count as number | undefined) ?? Number(log?.title.match(/^(\d+)/)?.[1] ?? 0);

  const measured = cited.some((e) => e.kind === "metric" || e.kind === "health");

  return [
    {
      ok: Boolean(trace),
      title: "Following requests step by step",
      text: trace
        ? `We followed ${total} ${slow ? "slow" : "failed"} requests through the system. ${matched} of them ${
            slow ? "spent most of their time" : "first broke"
          } at the same place: ${where}.`
        : "No request recordings pointed to a single place.",
    },
    {
      ok: Boolean(log),
      title: "Error messages",
      text: log
        ? `${displayName(log.service)} logged ${logCount}${logCount >= 100 ? "+" : ""} error messages about it.`
        : slow
          ? "No error messages: expected, because slow requests still succeed."
          : "No matching error messages were found.",
    },
    {
      ok: measured,
      title: "Monitoring numbers",
      text: measured
        ? "Error-rate, speed or health measurements show the same problem."
        : "The measurements don't clearly show it.",
    },
  ];
}

function mapStates(report: Report, top: Hypothesis | undefined) {
  const states: Partial<Record<NodeId, NodeState>> = {
    gateway: "ok",
    orders: "ok",
    inventory: "ok",
    database: "ok",
  };
  const notes: Partial<Record<NodeId, string>> = {};
  for (const s of report.symptoms) {
    const node = toNode(s.service);
    if (node) states[node] = "affected";
  }
  for (const hop of report.propagation) {
    const node = toNode(hop.service);
    if (node && hop.status !== "ok") states[node] = "affected";
  }
  const origin = top ? toNode(top.service) : null;
  if (origin) {
    states[origin] = "origin";
    notes[origin] = top!.failure_type.replace(/_/g, " ");
  }
  return { states, notes };
}

// ------------------------------------------------------------------ pieces

function Waterfall({ hops }: { hops: PropagationHop[] }) {
  const total = Math.max(...hops.map((h) => h.duration_ms), 1);
  return (
    <div className="waterfall">
      {hops.map((hop, index) => (
        <div key={index} className={`hop hop-${hop.status} ${hop.is_origin ? "origin" : ""}`}>
          <div className="hop-label" style={{ paddingLeft: `${index * 12}px` }}>
            <span className="hop-service">{hop.service}</span>
            <span className="muted small">
              {hop.kind} · {hop.operation}
            </span>
          </div>
          <div className="hop-bar-track">
            <div className="hop-bar" style={{ width: `${Math.max((hop.duration_ms / total) * 100, 1)}%` }} />
          </div>
          <div className="hop-meta small">{hop.duration_ms.toFixed(0)} ms</div>
          <div className="hop-meta small">
            {hop.error ? <span className="bad">{hop.error}</span> : hop.http_status ?? "ok"}
            {hop.is_origin && <strong className="bad"> ← started here</strong>}
          </div>
        </div>
      ))}
    </div>
  );
}

function EvidenceItem({ item, grafana }: { item: Evidence; grafana: string | null }) {
  const [open, setOpen] = useState(false);
  const samples = item.data.samples ?? [];
  return (
    <li className="evidence">
      <div className="evidence-head">
        <span className="chip">{item.id}</span>
        <span className="kind">{item.kind}</span>
        <strong>{item.title}</strong>
      </div>
      <p className="small">{item.detail}</p>
      {item.query && <pre className="query">{item.query}</pre>}
      {item.trace_id && grafana && (
        <a className="small" href={grafanaTraceUrl(grafana, item.trace_id)} target="_blank" rel="noreferrer">
          Open this request in Grafana ↗
        </a>
      )}
      {samples.length > 0 && (
        <div>
          <button className="link small" onClick={() => setOpen(!open)}>
            {open ? "Hide" : "Show"} {samples.length} log lines
          </button>
          {open && (
            <div className="table-scroll">
              <table className="table logs">
                <tbody>
                  {samples.map((entry, index) => (
                    <tr key={index}>
                      <td className="nowrap muted">{new Date(entry.timestamp_ns / 1e6).toLocaleTimeString()}</td>
                      <td>{entry.service}</td>
                      <td className={entry.level === "ERROR" ? "bad" : "muted"}>{entry.level}</td>
                      <td>{entry.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

// ------------------------------------------------------------------ report

function Story({ incident, report, grafana }: { incident: Incident; report: Report; grafana: string | null }) {
  const top = report.hypotheses[0];
  const { states, notes } = mapStates(report, top);
  const story = failureStory(report.propagation);
  const userSymptoms = report.symptoms.filter((s) => s.service === "gateway");
  const innerSymptoms = report.symptoms.filter((s) => s.service !== "gateway");
  const uncertainty = report.uncertainty.filter((n) => !n.startsWith("Confidence is a heuristic"));

  return (
    <>
      <section className="panel">
        <h2>
          <span className="section-num">1</span> What went wrong
        </h2>
        {report.symptoms.length === 0 ? (
          <p className="muted">Nothing unusual was measured in this time window.</p>
        ) : (
          <div className="impact-grid">
            <div>
              <h3>What users experienced</h3>
              {userSymptoms.length === 0 ? (
                <p className="muted small">No change measured at the front door.</p>
              ) : (
                <ul className="plain-list">
                  {userSymptoms.map((s, i) => (
                    <li key={i}>{describeSymptom(s)}</li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <h3>Inside the system</h3>
              <ul className="plain-list">
                {innerSymptoms.map((s, i) => (
                  <li key={i}>
                    <strong>{displayName(s.service)}:</strong> {describeSymptom(s)}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </section>

      {top && (
        <section className="panel">
          <h2>
            <span className="section-num">2</span> Where it started, and how it spread
          </h2>
          <p className="muted small">
            Red is where the problem started. Orange parts were only affected because they depend on it.
          </p>
          <SystemMap states={states} notes={notes} compact />
          {story.length > 0 && (
            <>
              <h3>One real failed request, step by step</h3>
              <ol className="story">
                {story.map((step, i) => (
                  <li key={i} className={`story-${step.tone}`}>
                    {step.text}
                  </li>
                ))}
              </ol>
            </>
          )}
        </section>
      )}

      {top && (
        <section className="panel">
          <h2>
            <span className="section-num">3</span> Why TraceOps thinks so
          </h2>
          <div className="checks">
            {evidenceChecks(top, report.evidence).map((check) => (
              <div key={check.title} className={`check ${check.ok ? "check-ok" : "check-no"}`}>
                <span className="check-icon">{check.ok ? "✓" : "–"}</span>
                <div>
                  <strong>{check.title}</strong>
                  <p className="small">{check.text}</p>
                </div>
              </div>
            ))}
          </div>
          <p className="muted small">
            Confidence score {top.confidence.toFixed(2)} out of 1. Following requests counts for 60%, and
            each other kind of evidence that agrees adds 20%. It's a simple rule of thumb, not a probability.
          </p>
        </section>
      )}

      <section className="panel">
        <h2>
          <span className="section-num">4</span> What we're not sure about
        </h2>
        {uncertainty.length === 0 ? (
          <p className="muted small">Nothing specific. All evidence sources were available and pointed the same way.</p>
        ) : (
          <ul className="plain-list">
            {uncertainty.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        )}
        {report.hypotheses.length > 1 && (
          <>
            <h3>Other possible causes</h3>
            <ul className="plain-list">
              {report.hypotheses.slice(1).map((h) => (
                <li key={h.rank}>
                  {verdict(h)} <span className="muted">({confidenceWords(h.confidence_label).toLowerCase()})</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      {top && (
        <section className="panel">
          <h2>
            <span className="section-num">5</span> What to check next
          </h2>
          <ul className="plain-list">
            {nextChecks(top).map((check, i) => (
              <li key={i}>{check}</li>
            ))}
          </ul>
        </section>
      )}

      <details className="panel technical">
        <summary>
          <strong>Technical details</strong> <span className="muted small">for engineers: raw evidence and exact queries</span>
        </summary>
        <p className="small">{report.summary}</p>
        <p className="muted small">
          Time window {formatTime(report.window_start)} → {formatTime(report.window_end)} ·{" "}
          {report.stats.traces_examined} traces examined ({report.stats.failed_traces} failed, {report.stats.slow_traces} slow) ·
          data sources:{" "}
          {Object.entries(report.data_sources)
            .map(([name, state]) => `${name} ${state === "ok" ? "✓" : "✗"}`)
            .join(", ")}
        </p>
        {incident.alerts.length > 0 && (
          <>
            <h3>Alarms received</h3>
            <div className="table-scroll">
              <table className="table small">
                <tbody>
                  {incident.alerts.map((alert) => (
                    <tr key={alert.fingerprint}>
                      <td>{alert.alertname}</td>
                      <td>{alert.service}</td>
                      <td>{alert.status}</td>
                      <td className="nowrap">{formatTime(alert.starts_at)}</td>
                      <td>{alert.summary}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
        {report.propagation.length > 0 && (
          <>
            <h3>Request path (spans of the example request)</h3>
            <Waterfall hops={report.propagation} />
          </>
        )}
        <h3>All evidence</h3>
        <ul className="evidence-list">
          {report.evidence.map((item) => (
            <EvidenceItem key={item.id} item={item} grafana={grafana} />
          ))}
        </ul>
      </details>
    </>
  );
}

// ------------------------------------------------------------------ page

export default function IncidentDetail({ id, config }: { id: number; config: PublicConfig | null }) {
  const { data: incident, error, refresh } = usePolling<Incident>(() => api.incident(id), 4000, [id]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [explaining, setExplaining] = useState(false);

  async function act(action: () => Promise<unknown>) {
    setActionError(null);
    try {
      await action();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
    refresh();
  }

  if (error && !incident) return <p className="error">Can't load incident: {error}</p>;
  if (!incident) return <p className="muted">Loading…</p>;

  const report = incident.report;
  const top = report?.hypotheses[0];
  const working = incident.investigation_status === "pending" || incident.investigation_status === "running";
  const llmEnabled = config?.explain_provider && config.explain_provider !== "none";
  const impact = report ? userImpact(report.symptoms) : null;

  return (
    <div className="detail">
      <a href="#/" className="back">
        ← Back
      </a>

      <section className="panel verdict-panel">
        <div className="incident-card-top">
          <span className={`pill pill-${working ? "investigating" : incident.status}`}>
            {working ? "Investigating" : incident.status === "open" ? "Ongoing" : "Resolved"}
          </span>
          <span className="muted small">
            Incident #{incident.id} · opened {timeAgo(incident.created_at)} (
            {incident.source === "alertmanager" ? "by a monitoring alarm" : "checked by hand"})
            {incident.resolved_at && ` · resolved ${timeAgo(incident.resolved_at)}`}
          </span>
        </div>
        <p className="eyebrow">Most likely cause</p>
        <h1 className="verdict">
          {top ? verdict(top) : working ? "Investigating…" : "No clear cause found"}
        </h1>
        {top && (
          <p>
            <span className={`conf conf-${top.confidence_label}`}>{confidenceWords(top.confidence_label)}</span>
            {impact && <span className="muted"> · Users: {impact}</span>}
          </p>
        )}
        <div className="button-row">
          <button className="secondary" onClick={() => act(() => api.reinvestigate(id))} disabled={working}>
            {working ? "Investigating…" : "Investigate again"}
          </button>
          {incident.status === "open" && (
            <button className="secondary" onClick={() => act(() => api.resolve(id))}>
              Mark as resolved
            </button>
          )}
        </div>
        {actionError && <p className="error">{actionError}</p>}
      </section>

      {working && <p className="notice">TraceOps is collecting evidence. This page updates by itself.</p>}
      {incident.investigation_status === "failed" && <p className="error">Investigation failed: {incident.error}</p>}

      {report && <Story incident={incident} report={report} grafana={config?.grafana_url ?? null} />}

      {report && (
        <section className="panel">
          <h2>AI explanation (optional)</h2>
          {incident.explanation ? (
            <>
              <p className="muted small">
                Written by {incident.explanation.model ?? incident.explanation.provider} from the evidence above.
                The evidence is the source of truth.
              </p>
              <pre className="explanation">{incident.explanation.text}</pre>
            </>
          ) : (
            <p className="muted small">
              {llmEnabled
                ? "Ask a local AI model to explain this incident in plain English."
                : "Turned off. Everything above is written directly from the evidence, no AI needed. The README explains how to turn on a free local model."}
            </p>
          )}
          {llmEnabled && (
            <button
              disabled={explaining}
              onClick={async () => {
                setExplaining(true);
                await act(() => api.explain(id));
                setExplaining(false);
              }}
            >
              {explaining ? "Writing…" : incident.explanation ? "Write again" : "Explain with AI"}
            </button>
          )}
        </section>
      )}
    </div>
  );
}
