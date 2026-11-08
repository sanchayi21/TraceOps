"""The investigation workflow.

Given a time window, the investigator:

1. METRICS  - asks Prometheus which services had elevated error rates, high
              latency, or were down, compared with the preceding window.
2. TRACES   - asks Tempo for failed and slow traces in the window, then finds
              where each failure ORIGINATED (see trace_analysis.py).
3. GROUPING - groups traces by failure point; the share of failed traces each
              failure point explains is the main confidence signal.
4. LOGS     - asks Loki for error logs from the implicated services, and for
              every log line belonging to one representative trace.
5. SCORING  - combines trace share with log and metric corroboration.
6. REPORT   - returns symptoms (observed), hypotheses (inferred), evidence,
              the propagation path, and explicit uncertainty notes.
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from traceops import trace_analysis as ta
from traceops.explain import build_summary
from traceops.models import (
    Evidence,
    Hypothesis,
    InvestigationReport,
    PropagationHop,
    Symptom,
)
from traceops.telemetry_clients import (
    LokiClient,
    PrometheusClient,
    TelemetryUnavailable,
    TempoClient,
)

MAX_TRACES = 40
ERROR_RATE_THRESHOLD = 0.05
LATENCY_THRESHOLD_S = 1.0
SLOW_SPAN_MS = 500
MIN_FAILED_SCRAPES = 3  # 3 x 5 s scrape interval = at least ~15 s unreachable

WEIGHT_TRACES, WEIGHT_LOGS, WEIGHT_METRICS = 0.6, 0.2, 0.2

FAILURE_LABELS = {
    "dependency_unavailable": "dependency unavailable",
    "service_unreachable": "service unreachable",
    "timeout": "timeout",
    "slow_dependency": "slow dependency",
    "slow_operation": "slow operation",
    "bad_response": "bad response",
    "internal_error": "internal error",
}


def confidence_label(value: float) -> str:
    if value >= 0.75:
        return "high"
    if value >= 0.45:
        return "medium"
    return "low"


def _location(span: ta.Span, service_hosts: dict[str, str]) -> tuple[str, str]:
    """(service, component) where a span's work actually happens."""
    if span.attributes.get("db.system"):
        db = span.attributes.get("peer.service") or span.attributes["db.system"]
        return db, f"{span.name} (from {span.service})"
    if span.kind == "client":
        # Time spent in an outgoing call with no child spans was spent waiting
        # on the callee (or on reaching it), so attribute it to the callee.
        target = ta.resolve_target(span, service_hosts)
        return target, f"{target} (HTTP from {span.service})"
    return span.service, span.name


class _EvidenceLog:
    def __init__(self):
        self.items: list[Evidence] = []

    def add(self, kind, title, detail, **fields) -> str:
        evidence_id = f"E{len(self.items) + 1}"
        self.items.append(
            Evidence(id=evidence_id, kind=kind, title=title, detail=detail, **fields)
        )
        return evidence_id


class Investigator:
    def __init__(
        self,
        prometheus: PrometheusClient,
        tempo: TempoClient,
        loki: LokiClient,
        service_urls: dict[str, str],
    ):
        self.prometheus = prometheus
        self.tempo = tempo
        self.loki = loki
        self.services = list(service_urls)
        # Map "host:port" and "host" -> service name, for resolving client spans.
        self.service_hosts = {name: name for name in service_urls}
        for name, url in service_urls.items():
            parsed = urlparse(url)
            self.service_hosts[f"{parsed.hostname}:{parsed.port}"] = name

    # ------------------------------------------------------------ public

    def investigate(self, start: datetime, end: datetime) -> InvestigationReport:
        evidence = _EvidenceLog()
        sources: dict[str, str] = {}
        uncertainty: list[str] = []

        symptoms = self._metric_symptoms(start, end, evidence, sources)
        traces = self._fetch_traces(start, end, sources)
        failed = [t for t in traces if t.failed]
        slow_ok = [
            t for t in traces
            if t.complete and not t.failed and t.duration_ms >= 1000
        ]

        failure_groups = self._group_failures(failed)
        slow_groups = self._group_slow(slow_ok)

        hypotheses, top_item = self._score(
            failure_groups, len(failed), slow_groups, len(slow_ok),
            symptoms, evidence, start, end, sources,
        )

        propagation, representative_id = [], None
        if top_item:
            trace, focus_span = top_item
            representative_id = trace.trace_id
            propagation = self._propagation(trace, focus_span)
            self._trace_logs(trace, start, end, evidence, sources)

        if not hypotheses and symptoms:
            hypotheses = [self._metrics_only_hypothesis(symptoms)]
            uncertainty.append(
                "No failed or slow traces were found for this window, so the "
                "likely failure point is inferred from metrics alone."
            )

        uncertainty += self._uncertainty_notes(
            sources, symptoms, failed, failure_groups, hypotheses, traces
        )

        report = InvestigationReport(
            window_start=start,
            window_end=end,
            generated_at=datetime.now(timezone.utc),
            summary="",
            symptoms=symptoms,
            hypotheses=hypotheses,
            propagation=propagation,
            representative_trace_id=representative_id,
            evidence=evidence.items,
            uncertainty=uncertainty,
            stats={
                "traces_examined": len(traces),
                "failed_traces": len(failed),
                "slow_traces": len(slow_ok),
                "distinct_failure_points": len(failure_groups),
            },
            data_sources=sources,
        )
        report.summary = build_summary(report)
        return report

    # ------------------------------------------------------------ metrics

    def _metric_symptoms(self, start, end, evidence, sources) -> list[Symptom]:
        window = max(60, int((end - start).total_seconds()))
        jobs = "|".join(self.services)
        q = {
            "error_ratio": (
                f'sum by (job) (increase(http_requests_total{{status=~"5.."}}[{window}s]))'
                f" / sum by (job) (increase(http_requests_total[{window}s]))"
            ),
            "requests": f"sum by (job) (increase(http_requests_total[{window}s]))",
            "p95": (
                "histogram_quantile(0.95, sum by (job, le) "
                f"(rate(http_request_duration_seconds_bucket[{window}s])))"
            ),
            # Number of failed scrapes (each scrape records up=1 or up=0).
            "failed_scrapes": (
                f'count_over_time(up{{job=~"{jobs}"}}[{window}s])'
                f' - sum_over_time(up{{job=~"{jobs}"}}[{window}s])'
            ),
        }
        baseline_q = {
            "error_ratio": q["error_ratio"].replace(
                f"[{window}s]", f"[{window}s] offset {window}s"
            ),
            "p95": q["p95"].replace(f"[{window}s]", f"[{window}s] offset {window}s"),
        }
        try:
            now = {name: self.prometheus.query(expr, end) for name, expr in q.items()}
            base = {
                name: self.prometheus.query(expr, end)
                for name, expr in baseline_q.items()
            }
            sources["prometheus"] = "ok"
        except TelemetryUnavailable as error:
            sources["prometheus"] = f"unavailable ({error})"
            return []

        symptoms = []
        for service in self.services:
            # One or two missed scrapes happen on restarts; require a sustained gap.
            failed = now["failed_scrapes"].get(service, 0)
            if failed >= MIN_FAILED_SCRAPES:
                eid = evidence.add(
                    "health", f"{service} failed {failed:.0f} Prometheus scrapes",
                    f"Prometheus could not reach {service}'s /metrics endpoint "
                    f"{failed:.0f} times (scrapes run every 5 s).",
                    service=service, query=q["failed_scrapes"],
                    data={"failed_scrapes": failed},
                )
                symptoms.append(Symptom(
                    service=service, signal="availability",
                    description=f"{service} was unreachable by Prometheus "
                    f"(~{failed * 5:.0f}s of failed scrapes)",
                    value=0, baseline=1, evidence_ids=[eid],
                ))

            ratio = now["error_ratio"].get(service)
            requests = now["requests"].get(service, 0)
            if ratio is not None and ratio >= ERROR_RATE_THRESHOLD and requests >= 1:
                baseline = base["error_ratio"].get(service, 0.0)
                eid = evidence.add(
                    "metric", f"{service} 5xx ratio {ratio:.0%}",
                    f"{ratio:.1%} of ~{requests:.0f} requests returned 5xx "
                    f"(previous window: {baseline:.1%}).",
                    service=service, query=q["error_ratio"],
                    data={"value": ratio, "baseline": baseline, "requests": requests},
                )
                symptoms.append(Symptom(
                    service=service, signal="error_rate",
                    description=f"{service} returned 5xx for {ratio:.0%} of requests "
                    f"(baseline {baseline:.0%})",
                    value=round(ratio, 4), baseline=round(baseline, 4),
                    evidence_ids=[eid],
                ))

            p95 = now["p95"].get(service)
            baseline_p95 = base["p95"].get(service)
            slow = p95 is not None and (
                p95 >= LATENCY_THRESHOLD_S
                or (baseline_p95 and p95 >= 0.3 and p95 >= 3 * baseline_p95)
            )
            if slow:
                eid = evidence.add(
                    "metric", f"{service} p95 latency {p95:.2f}s",
                    f"95th percentile latency was {p95:.2f}s "
                    f"(previous window: {baseline_p95 or 0:.2f}s).",
                    service=service, query=q["p95"],
                    data={"value": p95, "baseline": baseline_p95},
                )
                before = (
                    f"baseline {baseline_p95:.2f}s" if baseline_p95
                    else "no traffic in the previous window"
                )
                symptoms.append(Symptom(
                    service=service, signal="latency_p95",
                    description=f"{service} p95 latency {p95:.2f}s ({before})",
                    value=round(p95, 3),
                    baseline=round(baseline_p95, 3) if baseline_p95 else None,
                    evidence_ids=[eid],
                ))
        return symptoms

    # ------------------------------------------------------------ traces

    def _fetch_traces(self, start, end, sources) -> list[ta.ParsedTrace]:
        try:
            ids = self.tempo.search("{ status = error }", start, end, limit=30)
            ids += self.tempo.search(
                "{ kind = server && duration > 1s }", start, end, limit=20
            )
            unique_ids = list(dict.fromkeys(ids))[:MAX_TRACES]
            traces = [
                ta.parse_trace(trace_id, self.tempo.get_trace(trace_id))
                for trace_id in unique_ids
            ]
            sources["tempo"] = "ok"
            return traces
        except TelemetryUnavailable as error:
            sources["tempo"] = f"unavailable ({error})"
            return []

    def _group_failures(self, failed):
        groups: dict[ta.FailurePoint, list] = defaultdict(list)
        for trace in failed:
            origins = ta.find_error_origins(trace)
            if not origins:
                continue
            # If several, take the deepest (furthest from the root).
            origin = max(origins, key=lambda s: len(ta.path_to(trace, s)))
            point = ta.classify_origin(origin, self.service_hosts)
            focus = origin
            if point.failure_type == "timeout":
                bottleneck = ta.locate_bottleneck(origin)
                if (
                    bottleneck is not origin
                    and ta.self_time_ms(bottleneck) >= 0.5 * origin.duration_ms
                ):
                    service, component = _location(bottleneck, self.service_hosts)
                    point = ta.FailurePoint(
                        service=service,
                        component=component,
                        failure_type="slow_dependency",
                        detail=f"{origin.service} timed out after "
                        f"{origin.duration_ms:.0f} ms; downstream, "
                        f"'{bottleneck.name}' in {bottleneck.service} took "
                        f"{ta.self_time_ms(bottleneck):.0f} ms",
                    )
                    focus = bottleneck
            groups[point].append((trace, focus))
        return groups

    def _group_slow(self, slow_traces):
        groups: dict[ta.FailurePoint, list] = defaultdict(list)
        for trace in slow_traces:
            span = ta.slowest_span(trace.spans)
            if span is None:
                continue
            service, component = _location(span, self.service_hosts)
            point = ta.FailurePoint(
                service=service,
                component=component,
                failure_type="slow_dependency"
                if span.attributes.get("db.system") or span.kind == "client"
                else "slow_operation",
                detail=f"'{span.name}' in {span.service} accounts for "
                f"{ta.self_time_ms(span):.0f} ms of a "
                f"{trace.duration_ms:.0f} ms request",
            )
            groups[point].append((trace, span))
        return groups

    # ------------------------------------------------------------ logs

    def _error_logs(self, service, start, end, evidence, sources):
        query = f'{{service_name="{service}"}} | severity_text="ERROR"'
        try:
            entries = self.loki.query_range(query, start, end, limit=100)
            sources.setdefault("loki", "ok")
        except TelemetryUnavailable as error:
            sources["loki"] = f"unavailable ({error})"
            return None, 0
        if not entries:
            return None, 0
        common = Counter(entry["message"] for entry in entries).most_common(3)
        detail = "; ".join(f'"{message}" x{count}' for message, count in common)
        eid = evidence.add(
            "log", f"{len(entries)} error log lines from {service}",
            f"Most frequent: {detail}", service=service, query=query,
            data={"samples": entries[:10], "count": len(entries)},
        )
        return eid, len(entries)

    def _trace_logs(self, trace, start, end, evidence, sources):
        jobs = "|".join(self.services)
        query = f'{{service_name=~"{jobs}"}} | trace_id="{trace.trace_id}"'
        try:
            entries = self.loki.query_range(
                query, start - timedelta(minutes=1), end + timedelta(minutes=1)
            )
        except TelemetryUnavailable:
            return
        if entries:
            entries.sort(key=lambda entry: entry["timestamp_ns"])
            evidence.add(
                "log", f"{len(entries)} log lines share the representative trace_id",
                "Log lines from every service that handled this one request, "
                "joined to the trace by trace_id.",
                trace_id=trace.trace_id, query=query, data={"samples": entries},
            )

    # ------------------------------------------------------------ scoring

    def _score(self, failure_groups, n_failed, slow_groups, n_slow,
               symptoms, evidence, start, end, sources):
        by_service = defaultdict(set)
        for symptom in symptoms:
            by_service[symptom.service].add(symptom.signal)

        candidates = [
            (point, items, n_failed, "failed") for point, items in failure_groups.items()
        ]
        # Slow-but-successful traces only add a hypothesis when they point at
        # a service that isn't already part of the failing requests; otherwise
        # they are the same problem seen from a different angle.
        failing_services = {p.service for p in failure_groups} | {
            span.service
            for items in failure_groups.values()
            for trace, _ in items
            for span in trace.spans
        }
        candidates += [
            (point, items, n_slow, "slow") for point, items in slow_groups.items()
            if point.service not in failing_services
        ]

        # Failures outrank pure slowness; then by number of traces explained.
        candidates.sort(key=lambda c: (c[3] != "failed", -len(c[1])))

        hypotheses, top_item = [], None
        for rank, (point, items, total, kind) in enumerate(candidates[:5], start=1):
            trace, focus = max(items, key=lambda item: len(item[0].spans))
            share = len(items) / total
            samples = [t.trace_id for t, _ in items[:5]]
            trace_eid = evidence.add(
                "trace",
                f"{len(items)} of {total} {kind} traces point to {point.component}",
                point.detail,
                service=point.service,
                trace_id=trace.trace_id,
                query="{ status = error }" if kind == "failed"
                else "{ kind = server && duration > 1s }",
                data={"sample_trace_ids": samples, "matched": len(items),
                      "total": total, "trace_kind": kind},
            )
            evidence_ids = [trace_eid]

            # Log corroboration: errors logged by the service that observed it.
            observer = focus.service
            log_eid, log_count = self._error_logs(observer, start, end, evidence, sources)
            if log_eid:
                evidence_ids.append(log_eid)

            # Metric corroboration: a matching symptom on the implicated side.
            expected = {
                "service_unreachable": ({"availability"}, point.service),
                "slow_dependency": ({"latency_p95", "error_rate"}, observer),
                "slow_operation": ({"latency_p95"}, point.service),
                "timeout": ({"latency_p95", "error_rate"}, observer),
            }.get(point.failure_type, ({"error_rate"}, observer))
            signals, symptom_service = expected
            metric_hit = bool(by_service[symptom_service] & signals)
            if metric_hit:
                evidence_ids += [
                    eid for s in symptoms
                    if s.service == symptom_service and s.signal in signals
                    for eid in s.evidence_ids
                ]

            confidence = round(
                WEIGHT_TRACES * share
                + WEIGHT_LOGS * (1 if log_eid else 0)
                + WEIGHT_METRICS * (1 if metric_hit else 0),
                2,
            )
            reasoning = (
                f"{point.detail}. Explains {len(items)}/{total} {kind} traces "
                f"({share:.0%}); "
                + (f"{log_count} error log lines from {observer}; " if log_eid
                   else f"no error logs found from {observer}; ")
                + ("matching metric symptom present." if metric_hit
                   else "no matching metric symptom.")
            )
            hypotheses.append(Hypothesis(
                rank=rank,
                service=point.service,
                component=point.component,
                failure_type=point.failure_type,
                confidence=confidence,
                confidence_label=confidence_label(confidence),
                reasoning=reasoning,
                evidence_ids=evidence_ids,
            ))
            if rank == 1:
                top_item = (trace, focus)
        return hypotheses, top_item

    def _metrics_only_hypothesis(self, symptoms) -> Hypothesis:
        order = {"availability": 0, "error_rate": 1, "latency_p95": 2}
        # Prefer the most downstream service (later in the configured list).
        best = sorted(
            symptoms,
            key=lambda s: (order[s.signal], -self.services.index(s.service)
                           if s.service in self.services else 0),
        )[0]
        return Hypothesis(
            rank=1,
            service=best.service,
            component=best.service,
            failure_type={"availability": "service_unreachable",
                          "error_rate": "internal_error",
                          "latency_p95": "slow_operation"}[best.signal],
            confidence=0.3,
            confidence_label="low",
            reasoning=f"Metrics only: {best.description}. Without traces, "
            "upstream and downstream failures cannot be told apart.",
            evidence_ids=best.evidence_ids,
        )

    # ------------------------------------------------------------ report parts

    def _propagation(self, trace, focus) -> list[PropagationHop]:
        hops = []
        for span in ta.path_to(trace, focus):
            self_ms = ta.self_time_ms(span)
            status = "error" if span.is_error else (
                "slow" if self_ms >= SLOW_SPAN_MS else "ok"
            )
            error = None
            if span.is_error:
                error = span.exception_type or (
                    f"HTTP {span.http_status}" if span.http_status else "error"
                )
            hops.append(PropagationHop(
                service=span.service, operation=span.name, kind=span.kind,
                status=status, http_status=span.http_status,
                duration_ms=round(span.duration_ms, 1),
                self_time_ms=round(self_ms, 1),
                is_origin=span is focus, error=error,
            ))
        return hops

    def _uncertainty_notes(self, sources, symptoms, failed, failure_groups,
                           hypotheses, traces) -> list[str]:
        notes = [
            f"{name.capitalize()} was {state}; evidence from it is missing."
            for name, state in sources.items() if state != "ok"
        ]
        if any(s.signal == "error_rate" for s in symptoms) and not failed:
            notes.append(
                "Metrics show errors but no failed traces were found. Traces may "
                "not be indexed yet, or the failures happened outside the window."
            )
        if len(failure_groups) > 1:
            notes.append(
                f"{len(failure_groups)} distinct failure points were seen; there "
                "may be more than one concurrent problem."
            )
        if len(traces) >= MAX_TRACES:
            notes.append(f"Only the first {MAX_TRACES} matching traces were examined.")
        types = {h.failure_type for h in hypotheses[:1]}
        if "service_unreachable" in types:
            notes.append(
                "From the caller's side, a crashed process, a stopped container "
                "and a network problem look the same. Check the service's process "
                "or container status to confirm."
            )
        if "slow_dependency" in types:
            notes.append(
                "Spans show where time was spent, not why. A slow query can come "
                "from locking, load or resource limits on the database."
            )
        if not symptoms and not hypotheses:
            notes.append("No abnormal metrics or failing traces were found in this window.")
        notes.append(
            "Confidence is a heuristic score (60% share of traces explained, 20% "
            "log corroboration, 20% metric corroboration), not a probability."
        )
        return notes
