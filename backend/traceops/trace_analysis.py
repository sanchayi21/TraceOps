"""Pure functions that turn a raw OpenTelemetry trace into failure evidence.

No network calls here, so this logic is easy to unit-test and explain.

Key ideas
---------
* A trace is a tree of spans. Each span has a parent (except the root).
* When a request fails, the error usually *propagates upwards*: the database
  span fails, then the inventory server span, then the orders client span,
  and so on up to the gateway. Every one of those spans is marked ERROR.
* The span where the failure ORIGINATED is the deepest error span: an error
  span with no error spans beneath it. We call it the "error origin".
* For slow requests, the useful number is a span's SELF TIME: its duration
  minus the time covered by its children. A span that is slow only because
  its child is slow has a small self time; the real bottleneck has a large one.
"""

import base64
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class Span:
    span_id: str
    parent_id: str | None
    service: str
    name: str
    kind: str  # "server", "client", "internal", ...
    start_ns: int
    end_ns: int
    attributes: dict
    is_error: bool
    status_message: str = ""
    exception_type: str | None = None
    exception_message: str | None = None
    children: list["Span"] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1e6

    @property
    def http_status(self) -> int | None:
        value = self.attributes.get("http.status_code") or self.attributes.get(
            "http.response.status_code"
        )
        return int(value) if value is not None else None

    @property
    def target_host(self) -> str | None:
        """Hostname this CLIENT span called (e.g. 'inventory' or 'postgres')."""
        url = self.attributes.get("http.url") or self.attributes.get("url.full")
        if url:
            return urlparse(url).hostname
        return self.attributes.get("server.address")

    def describe(self) -> str:
        return f"{self.service}: {self.name}"


@dataclass
class ParsedTrace:
    trace_id: str
    spans: list[Span]
    root: Span | None

    @property
    def failed(self) -> bool:
        """A trace counts as failed when the user-facing (root) span failed
        with a server error. 4xx responses such as 404 are client mistakes,
        not incidents."""
        if self.root is None:
            return False
        status = self.root.http_status
        return (status is not None and status >= 500) or (
            status is None and self.root.is_error
        )

    @property
    def duration_ms(self) -> float:
        return self.root.duration_ms if self.root else 0.0

    @property
    def complete(self) -> bool:
        """False when the true root span is missing (not yet received by
        Tempo), in which case durations and 'root' are misleading."""
        return self.root is not None and self.root.parent_id is None


# ---------------------------------------------------------------- parsing


def _decode_id(value: str | None) -> str | None:
    """Tempo returns IDs base64-encoded; we display them as hex."""
    if not value:
        return None
    try:
        return base64.b64decode(value).hex()
    except ValueError:
        return value


def _attr_value(value: dict):
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    return None


def _attributes(raw: list[dict] | None) -> dict:
    return {item["key"]: _attr_value(item.get("value", {})) for item in raw or []}


def parse_trace(trace_id: str, payload: dict) -> ParsedTrace:
    """Parse Tempo's OTLP JSON (`/api/traces/<id>`) into linked Span objects."""
    batches = payload.get("batches") or payload.get("resourceSpans") or []
    if "trace" in payload:  # /api/v2/traces shape
        batches = payload["trace"].get("resourceSpans", [])

    spans: list[Span] = []
    for batch in batches:
        service = _attributes(batch.get("resource", {}).get("attributes")).get(
            "service.name", "unknown"
        )
        scope_groups = batch.get("scopeSpans") or batch.get(
            "instrumentationLibrarySpans", []
        )
        for group in scope_groups:
            for raw in group.get("spans", []):
                status = raw.get("status") or {}
                exception_type = exception_message = None
                for event in raw.get("events", []) or []:
                    if event.get("name") == "exception":
                        event_attrs = _attributes(event.get("attributes"))
                        exception_type = event_attrs.get("exception.type")
                        exception_message = event_attrs.get("exception.message")
                spans.append(
                    Span(
                        span_id=_decode_id(raw.get("spanId")),
                        parent_id=_decode_id(raw.get("parentSpanId")),
                        service=service,
                        name=raw.get("name", ""),
                        kind=raw.get("kind", "").replace("SPAN_KIND_", "").lower(),
                        start_ns=int(raw.get("startTimeUnixNano", 0)),
                        end_ns=int(raw.get("endTimeUnixNano", 0)),
                        attributes=_attributes(raw.get("attributes")),
                        is_error=status.get("code") in ("STATUS_CODE_ERROR", 2),
                        status_message=status.get("message", ""),
                        exception_type=exception_type,
                        exception_message=exception_message,
                    )
                )

    by_id = {span.span_id: span for span in spans}
    root = None
    for span in spans:
        parent = by_id.get(span.parent_id)
        if parent:
            parent.children.append(span)
        elif root is None or span.start_ns < root.start_ns:
            root = span
    for span in spans:
        span.children.sort(key=lambda child: child.start_ns)
    return ParsedTrace(trace_id=trace_id, spans=spans, root=root)


# ---------------------------------------------------------------- analysis


def _has_error_descendant(span: Span) -> bool:
    return any(child.is_error or _has_error_descendant(child) for child in span.children)


def find_error_origins(trace: ParsedTrace) -> list[Span]:
    """Error spans that have no error spans beneath them."""
    return [s for s in trace.spans if s.is_error and not _has_error_descendant(s)]


def self_time_ms(span: Span) -> float:
    """Duration not covered by any child span (overlapping children merged)."""
    intervals = sorted(
        (max(c.start_ns, span.start_ns), min(c.end_ns, span.end_ns))
        for c in span.children
    )
    covered, cursor = 0, span.start_ns
    for start, end in intervals:
        start = max(start, cursor)
        if end > start:
            covered += end - start
            cursor = end
    return max(0.0, (span.end_ns - span.start_ns - covered) / 1e6)


def slowest_span(spans: list[Span]) -> Span | None:
    """The span with the largest self time: where the time was actually spent."""
    return max(spans, key=self_time_ms, default=None)


def subtree(span: Span) -> list[Span]:
    result = [span]
    for child in span.children:
        result.extend(subtree(child))
    return result


def path_to(trace: ParsedTrace, target: Span) -> list[Span]:
    """Spans from the root down to `target` (the propagation path)."""
    by_id = {span.span_id: span for span in trace.spans}
    path, current = [], target
    while current is not None:
        path.append(current)
        current = by_id.get(current.parent_id)
    return list(reversed(path))


@dataclass(frozen=True)
class FailurePoint:
    """A classified failure origin. FailurePoints with the same service,
    component and failure_type are grouped together; `detail` (which varies
    with exact error messages) is ignored when comparing."""

    service: str  # service where the problem is located
    component: str  # e.g. "postgresql", "inventory (HTTP)", "GET /inventory/{product_id}"
    failure_type: str
    detail: str = field(compare=False)


def resolve_target(span: Span, service_hosts: dict[str, str]) -> str:
    """Name of the service a CLIENT span called.

    `service_hosts` maps "host:port" and "host" to service names, e.g.
    {"inventory:8001": "inventory", "127.0.0.1:8001": "inventory"}.
    """
    url = span.attributes.get("http.url") or span.attributes.get("url.full")
    if url:
        parsed = urlparse(url)
        for key in (f"{parsed.hostname}:{parsed.port}", parsed.hostname):
            if key in service_hosts:
                return service_hosts[key]
        return parsed.netloc or "unknown"
    return span.target_host or "unknown"


def classify_origin(origin: Span, service_hosts: dict[str, str]) -> FailurePoint:
    """Decide what kind of failure an error-origin span represents."""
    exc = origin.exception_type or ""
    exc_message = origin.exception_message or origin.status_message or ""

    if origin.kind == "client" and origin.attributes.get("db.system"):
        db = origin.attributes.get("peer.service") or origin.attributes["db.system"]
        return FailurePoint(
            service=db,
            component=f"{db} (called by {origin.service})",
            failure_type="dependency_unavailable",
            detail=f"'{origin.name}' failed in {origin.service}: "
            f"{exc or 'error'} {exc_message}".strip(),
        )

    if origin.kind == "client":
        target = resolve_target(origin, service_hosts)
        # ConnectError / ConnectTimeout: no connection was ever established,
        # so the callee is unreachable (stopped, crashed, DNS or network).
        if "Connect" in exc or (not origin.children and exc and "Timeout" not in exc):
            return FailurePoint(
                service=target,
                component=f"{target} (HTTP from {origin.service})",
                failure_type="service_unreachable",
                detail=f"{origin.service} could not connect to {target} ({exc})",
            )
        # ReadTimeout etc.: connected, but the callee answered too slowly.
        if "Timeout" in exc:
            return FailurePoint(
                service=target,
                component=f"{target} (HTTP from {origin.service})",
                failure_type="timeout",
                detail=f"{origin.service} timed out calling {target} ({exc})",
            )
        if not origin.children:
            # No span from the callee exists at all: the request never arrived.
            return FailurePoint(
                service=target,
                component=f"{target} (HTTP from {origin.service})",
                failure_type="service_unreachable",
                detail=f"{origin.service} could not connect to {target}"
                + (f" ({exc})" if exc else ""),
            )
        return FailurePoint(
            service=target,
            component=f"{target} (HTTP from {origin.service})",
            failure_type="bad_response",
            detail=f"{target} returned HTTP {origin.http_status} to {origin.service}",
        )

    return FailurePoint(
        service=origin.service,
        component=origin.name,
        failure_type="internal_error",
        detail=f"{origin.service} '{origin.name}' failed"
        + (f" with HTTP {origin.http_status}" if origin.http_status else "")
        + (f": {exc} {exc_message}" if exc else ""),
    )


def locate_bottleneck(span: Span) -> Span:
    """Within a span's subtree, the span with the most self time."""
    return slowest_span(subtree(span)) or span
