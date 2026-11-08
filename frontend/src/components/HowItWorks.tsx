import SystemMap from "./SystemMap";

const STEPS = [
  {
    title: "The services report what they're doing",
    text: "Every service automatically records three kinds of evidence while it works. Measurements: how many requests, how many failed, how long they took. Error messages: what went wrong, in words. Request recordings: the exact path of each request through every service, with timings.",
    tech: "OpenTelemetry → Prometheus (measurements), Loki (logs), Tempo (traces)",
  },
  {
    title: "An alarm goes off",
    text: "Monitoring checks the measurements every few seconds. If too many requests fail, or they get too slow, or a service stops answering, an alarm is raised and sent to TraceOps.",
    tech: "Prometheus alert rules → Alertmanager → TraceOps webhook",
  },
  {
    title: "TraceOps investigates",
    text: "It gathers all three kinds of evidence from around the time of the alarm. Then it follows failed requests step by step. Errors travel upwards (the database fails, so Inventory fails, so Orders fails, so the Gateway fails), so the deepest failing step is where the problem started.",
    tech: "Python + FastAPI, backend/traceops/investigator.py",
  },
  {
    title: "You get a clear report",
    text: "What users experienced, where the problem started, how it spread, the evidence behind it, how confident TraceOps is, what it's unsure about, and what to check next.",
    tech: "Stored in PostgreSQL, shown in this React dashboard",
  },
];

const GLOSSARY = [
  ["Service", "One small program that does one job (e.g. Orders). Apps are built from many services that call each other."],
  ["Incident", "A period where something is broken for users."],
  ["Root cause", "The thing that actually broke, as opposed to the parts that only failed because of it."],
  ["Metric", "A number measured over time, e.g. '5% of requests failed in the last minute'."],
  ["Log", "A line of text a program writes about what it's doing, e.g. 'Database unavailable'."],
  ["Trace", "A recording of one request's whole journey through all the services, with timings for each step."],
  ["Span", "One step inside a trace, e.g. 'Inventory ran a database query, took 1.5 s'."],
  ["Alert", "An automatic alarm when a metric crosses a limit, e.g. more than 20% of requests failing."],
  ["Timeout", "When a service stops waiting for an answer because it took too long."],
  ["HTTP 5xx", "Error codes meaning 'the server failed' (500, 502, 503, 504), as opposed to 404 'not found'."],
];

export default function HowItWorks() {
  return (
    <div className="detail">
      <a href="#/" className="back">
        ← Back
      </a>
      <section className="hero">
        <h1>How TraceOps works</h1>
        <p>
          The demo shop below is a tiny version of how real apps are built. A user request enters
          at the Gateway and passes through each service to the database. If any part breaks, every
          part before it shows errors too. TraceOps works out which one really broke.
        </p>
        <SystemMap states={{ gateway: "ok", orders: "ok", inventory: "ok", database: "ok" }} compact />
      </section>

      <section className="panel">
        <h2>The 4 steps</h2>
        <div className="how-steps">
          {STEPS.map((step, i) => (
            <div key={step.title} className="how-step">
              <div className="step-num">{i + 1}</div>
              <div>
                <h3>{step.title}</h3>
                <p>{step.text}</p>
                <p className="muted small">Built with: {step.tech}</p>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="panel">
        <h2>Real or simulated?</h2>
        <ul className="plain-list">
          <li>
            <strong>Real:</strong> the services, the database, the monitoring tools, the alarms, the evidence
            and the investigation. These are the same open-source tools companies use.
          </li>
          <li>
            <strong>Simulated:</strong> the customers (a traffic generator) and the failures (switched on by
            the "break something" buttons). The failures themselves are genuine, though: "database offline"
            really fails to connect, and "slow database" really runs a slow query.
          </li>
          <li>TraceOps is never told which failure you chose. It only sees the evidence.</li>
        </ul>
      </section>

      <section className="panel">
        <h2>Words you'll see</h2>
        <dl className="glossary">
          {GLOSSARY.map(([term, meaning]) => (
            <div key={term}>
              <dt>{term}</dt>
              <dd>{meaning}</dd>
            </div>
          ))}
        </dl>
      </section>
    </div>
  );
}
