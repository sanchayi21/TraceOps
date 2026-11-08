import type { DemoStatus, Incident, IncidentSummary, PublicConfig } from "./types";

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const json = await response.json();
      detail = typeof json.detail === "string" ? json.detail : JSON.stringify(json.detail);
    } catch {
      /* not JSON */
    }
    throw new Error(`${response.status}: ${detail}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  config: () => request<PublicConfig>("GET", "/api/config"),
  incidents: () => request<IncidentSummary[]>("GET", "/api/incidents"),
  incident: (id: number) => request<Incident>("GET", `/api/incidents/${id}`),
  investigate: (minutes: number) =>
    request<Incident>("POST", "/api/investigations", { minutes }),
  reinvestigate: (id: number) =>
    request<Incident>("POST", `/api/incidents/${id}/reinvestigate`),
  resolve: (id: number) => request<Incident>("POST", `/api/incidents/${id}/resolve`),
  explain: (id: number) => request<Incident>("POST", `/api/incidents/${id}/explain`),

  demoStatus: () => request<DemoStatus>("GET", "/api/demo/status"),
  startTraffic: (duration_seconds: number) =>
    request("POST", "/api/demo/traffic", { duration_seconds, requests_per_second: 4 }),
  stopTraffic: () => request("DELETE", "/api/demo/traffic"),
  injectFault: (fault: { mode: string; latency_ms?: number; error_rate?: number }) =>
    request("PUT", "/api/demo/fault", fault),
  clearFault: () => request("DELETE", "/api/demo/fault"),
};
