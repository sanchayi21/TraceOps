// Mirrors backend/traceops/models.py. Keep the two in sync.

export interface Evidence {
  id: string;
  kind: "metric" | "trace" | "log" | "health";
  service: string | null;
  title: string;
  detail: string;
  query: string | null;
  trace_id: string | null;
  data: {
    samples?: LogEntry[];
    sample_trace_ids?: string[];
    [key: string]: unknown;
  };
}

export interface LogEntry {
  timestamp_ns: number;
  service: string | null;
  level: string | null;
  message: string;
  trace_id: string | null;
}

export interface Symptom {
  service: string;
  signal: "error_rate" | "latency_p95" | "availability";
  description: string;
  value: number | null;
  baseline: number | null;
  evidence_ids: string[];
}

export interface Hypothesis {
  rank: number;
  service: string;
  component: string;
  failure_type: string;
  confidence: number;
  confidence_label: "high" | "medium" | "low";
  reasoning: string;
  evidence_ids: string[];
}

export interface PropagationHop {
  service: string;
  operation: string;
  kind: string;
  status: "error" | "ok" | "slow";
  http_status: number | null;
  duration_ms: number;
  self_time_ms: number;
  is_origin: boolean;
  error: string | null;
}

export interface Report {
  window_start: string;
  window_end: string;
  generated_at: string;
  summary: string;
  symptoms: Symptom[];
  hypotheses: Hypothesis[];
  propagation: PropagationHop[];
  representative_trace_id: string | null;
  evidence: Evidence[];
  uncertainty: string[];
  stats: Record<string, number>;
  data_sources: Record<string, string>;
}

export interface AlertRecord {
  fingerprint: string;
  alertname: string;
  service: string | null;
  severity: string;
  summary: string;
  status: "firing" | "resolved";
  starts_at: string;
  ends_at: string | null;
}

export interface Incident {
  id: number;
  title: string;
  status: "open" | "resolved";
  investigation_status: "pending" | "running" | "completed" | "failed";
  severity: string;
  source: "alertmanager" | "manual";
  services: string[];
  window_start: string;
  window_end: string;
  alerts: AlertRecord[];
  report: Report | null;
  explanation: { provider: string; model: string | null; text: string; generated_at: string } | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
}

export interface IncidentSummary {
  id: number;
  title: string;
  status: string;
  investigation_status: string;
  severity: string;
  source: string;
  services: string[];
  top_hypothesis: string | null;
  confidence_label: string | null;
  top_service: string | null;
  top_failure_type: string | null;
  top_confidence: number | null;
  symptoms: Symptom[];
  created_at: string;
  resolved_at: string | null;
}

export interface DemoStatus {
  services: {
    name: string;
    url: string;
    healthy: boolean;
    fault: { mode: string; latency_ms: number; error_rate: number } | null;
  }[];
  traffic: {
    active: boolean;
    seconds_left: number;
    sent: number;
    status_counts: Record<string, number>;
  };
}

export interface PublicConfig {
  explain_provider: string;
  services: string[];
  grafana_url: string;
}
