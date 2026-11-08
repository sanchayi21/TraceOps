// Turns technical findings into plain-language sentences for the UI.
// Everything here only REWORDS what the backend report says; it never adds facts.

import type { Hypothesis, PropagationHop, Symptom } from "./types";

export type NodeId = "gateway" | "orders" | "inventory" | "database";

export const NODES: { id: NodeId; name: string; role: string }[] = [
  { id: "gateway", name: "Gateway", role: "front door: receives every user request" },
  { id: "orders", name: "Orders", role: "handles orders, asks Inventory for stock" },
  { id: "inventory", name: "Inventory", role: "looks up stock in the database" },
  { id: "database", name: "Database", role: "PostgreSQL: stores the products" },
];

/** Map a service name from the report to a box on the system map. */
export function toNode(service: string | null | undefined): NodeId | null {
  if (!service) return null;
  if (service === "postgresql" || service === "postgres") return "database";
  if (service === "gateway" || service === "orders" || service === "inventory") return service;
  return null;
}

export function displayName(service: string | null | undefined): string {
  const node = toNode(service);
  return node ? NODES.find((n) => n.id === node)!.name : service ?? "Unknown";
}

/** One-line headline: what broke. */
export function verdict(h: Pick<Hypothesis, "service" | "failure_type">): string {
  const name = displayName(h.service);
  switch (h.failure_type) {
    case "dependency_unavailable":
      return h.service === "postgresql"
        ? "The database stopped accepting connections"
        : `${name} stopped accepting connections`;
    case "service_unreachable":
      return `The ${name} service is down or unreachable`;
    case "slow_dependency":
    case "slow_operation":
      return h.service === "postgresql"
        ? "Database queries became very slow"
        : `The ${name} service became very slow`;
    case "timeout":
      return `The ${name} service is too slow to answer in time`;
    case "internal_error":
      return `The ${name} service is crashing on requests`;
    case "bad_response":
      return `The ${name} service is returning errors`;
    default:
      return `A problem in ${name}`;
  }
}

export function confidenceWords(label: string | null | undefined): string {
  if (label === "high") return "Very likely";
  if (label === "medium") return "Likely";
  if (label === "low") return "Possible";
  return "";
}

/** Plain description of one measured symptom. */
export function describeSymptom(s: Symptom): string {
  if (s.signal === "error_rate") {
    const now = Math.round((s.value ?? 0) * 100);
    const before = Math.round((s.baseline ?? 0) * 100);
    return `${now}% of requests failed` + (before ? ` (before: ${before}%)` : " (before: none)");
  }
  if (s.signal === "latency_p95") {
    const before = s.baseline ? ` (before: ${s.baseline.toFixed(2)} s)` : "";
    return `Slow requests took ${(s.value ?? 0).toFixed(1)} s or more${before}`;
  }
  return "Stopped answering health checks";
}

/** The most user-relevant impact line: what the gateway (the user) experienced. */
export function userImpact(symptoms: Symptom[]): string | null {
  const gateway = symptoms.filter((s) => s.service === "gateway");
  const pick =
    gateway.find((s) => s.signal === "error_rate") ??
    gateway.find((s) => s.signal === "latency_p95") ??
    symptoms[0];
  return pick ? describeSymptom(pick) : null;
}

const HTTP_NAMES: Record<number, string> = {
  200: "OK",
  404: "Not Found",
  500: "Internal Server Error",
  502: "Bad Gateway",
  503: "Service Unavailable",
  504: "Gateway Timeout",
};

function httpText(status: number | null): string {
  if (status === null) return "";
  return `${status} ${HTTP_NAMES[status] ?? ""}`.trim();
}

function seconds(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

function errorText(error: string | null): string {
  if (!error) return "failed";
  if (error.includes("ConnectTimeout") || error.includes("ConnectError")) return "could not connect";
  if (error.includes("ReadTimeout")) return "gave up waiting (timeout)";
  if (error.includes("OperationalError")) return "connection refused";
  return error;
}

export interface StoryStep {
  node: NodeId | null;
  text: string;
  tone: "origin" | "bad" | "slow" | "ok";
}

/**
 * The failure as a story, from where it started up to what the user saw.
 * Built from the representative trace's path (root ... origin).
 */
export function failureStory(hops: PropagationHop[]): StoryStep[] {
  if (hops.length === 0) return [];
  const steps: StoryStep[] = [];
  const origin = hops[hops.length - 1];

  // 1. Where it started
  const isDb = origin.operation.startsWith("postgresql") || origin.operation.startsWith("SELECT");
  if (isDb) {
    steps.push({
      node: "database",
      tone: "origin",
      text:
        origin.status === "error"
          ? `Inventory tried to talk to the Database: ${errorText(origin.error)}.`
          : `A database query ("${origin.operation}") took ${seconds(origin.self_time_ms)}.`,
    });
  } else if (origin.kind === "client") {
    const caller = displayName(origin.service);
    const callee = NODES[NODES.findIndex((n) => n.id === toNode(origin.service)) + 1]?.name ?? "the next service";
    steps.push({
      node: NODES.find((n) => n.name === callee)?.id ?? null,
      tone: "origin",
      text: `${caller} tried to call ${callee}: ${errorText(origin.error)} after ${seconds(origin.duration_ms)}.`,
    });
  } else {
    steps.push({
      node: toNode(origin.service),
      tone: "origin",
      text: `${displayName(origin.service)} failed while handling the request (${origin.error ?? httpText(origin.http_status)}).`,
    });
  }

  // 2. How each service passed it upwards (server spans, deepest first)
  const servers = hops.filter((h) => h.kind === "server" && h !== origin).reverse();
  servers.forEach((hop, index) => {
    const isLast = index === servers.length - 1;
    const name = displayName(hop.service);
    const ok = hop.http_status !== null && hop.http_status < 400;
    // Did this service give up waiting on the service it called?
    const gaveUp = hops.find(
      (h) => h.service === hop.service && h.kind === "client" && h.error?.includes("ReadTimeout"),
    );
    const answer = ok
      ? `answered OK but only after ${seconds(hop.duration_ms)}`
      : gaveUp
        ? `gave up waiting after ${seconds(gaveUp.duration_ms)} and answered with an error: ${httpText(hop.http_status)}`
        : `answered with an error: ${httpText(hop.http_status)}`;
    steps.push({
      node: toNode(hop.service),
      tone: ok ? (hop.duration_ms >= 1000 ? "slow" : "ok") : "bad",
      text: isLast ? `The user got: ${ok ? `a response after ${seconds(hop.duration_ms)}` : `error ${httpText(hop.http_status)}`}.` : `${name} ${answer}.`,
    });
  });
  return steps;
}

/** What an engineer should check next, by failure type. */
export function nextChecks(h: Pick<Hypothesis, "service" | "failure_type">): string[] {
  const svc = h.service === "postgresql" ? "postgres" : h.service;
  switch (h.failure_type) {
    case "dependency_unavailable":
      return [
        `Is the database running? → docker compose ps ${svc}`,
        `Read the database's own logs → docker compose logs ${svc}`,
        "Check Inventory's database settings (host, port, password).",
      ];
    case "service_unreachable":
      return [
        `Is the service running? → docker compose ps ${svc}`,
        `Read its logs for a crash → docker compose logs ${svc}`,
        `Start it again → docker compose start ${svc}`,
      ];
    case "slow_dependency":
    case "slow_operation":
    case "timeout":
      return [
        "Open the example request in Grafana to see where the time went.",
        "Look for slow or blocked queries (Postgres: SELECT * FROM pg_stat_activity).",
        "Check CPU and memory use → docker stats",
      ];
    default:
      return [
        `Read the service's error logs → docker compose logs ${svc}`,
        "Open the example request in Grafana.",
        "Check what changed recently (deploys, configuration).",
      ];
  }
}

export function timeAgo(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}
