"""Shared observability setup for the demo services.

Every service calls `setup_telemetry(app, "<name>")` once. That gives it the
three signals TraceOps investigates:

1. Traces  - OpenTelemetry spans, exported over OTLP/HTTP to Grafana Tempo.
             FastAPI (incoming) and httpx (outgoing) are instrumented
             automatically, and httpx adds the W3C `traceparent` header, so a
             single request keeps ONE trace_id across gateway -> orders ->
             inventory.
2. Logs    - Python `logging` records, printed as JSON to stdout AND exported
             over OTLP to Grafana Loki. Each record carries trace_id/span_id,
             which is how TraceOps joins a log line to a trace.
3. Metrics - Prometheus counters/histograms served at GET /metrics and scraped
             by Prometheus. Alert rules are written against these.

Set TELEMETRY_EXPORT=false to keep everything local (used by the tests).
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# --- Prometheus metrics -----------------------------------------------------
# One counter and one histogram, labelled by route and status code. The
# `service` label is added by Prometheus itself (the scrape job name).
REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled by this service",
    ["method", "route", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "Time spent handling HTTP requests",
    ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10),
)


class JsonFormatter(logging.Formatter):
    """Formats a log record as one JSON object per line (structured logging)."""

    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc)
        entry = {
            "timestamp": timestamp.isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "message": record.getMessage(),
        }
        if span_context.is_valid:
            entry["trace_id"] = format(span_context.trace_id, "032x")
            entry["span_id"] = format(span_context.span_id, "016x")
        for key, value in getattr(record, "__dict__", {}).items():
            if key.startswith("ctx_"):
                entry[key[4:]] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_telemetry(app: FastAPI, service: str) -> logging.Logger:
    export = os.getenv("TELEMETRY_EXPORT", "true").lower() == "true"
    resource = Resource.create(
        {"service.name": service, "deployment.environment": "local-demo"}
    )

    # Traces
    tracer_provider = TracerProvider(resource=resource)
    if export:
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=os.getenv(
                        "OTEL_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces"
                    )
                )
            )
        )
    trace.set_tracer_provider(tracer_provider)

    # Logs: JSON to stdout always, OTLP to Loki when exporting.
    logger = logging.getLogger(service)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(JsonFormatter(service))
    logger.addHandler(stdout_handler)
    if export:
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(
                    endpoint=os.getenv(
                        "OTEL_LOGS_ENDPOINT", "http://127.0.0.1:3100/otlp/v1/logs"
                    )
                )
            )
        )
        logger.addHandler(LoggingHandler(logger_provider=logger_provider))

    # Automatic spans for incoming FastAPI requests and outgoing httpx calls.
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        excluded_urls="health,metrics,chaos",
        exclude_spans=["receive", "send"],
    )
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)

    @app.middleware("http")
    async def record_metrics(request: Request, call_next):
        # Housekeeping endpoints are not user traffic; counting them would
        # dilute error ratios (Docker calls /health every few seconds).
        if request.url.path in ("/metrics", "/health", "/chaos"):
            return await call_next(request)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            route_path = getattr(route, "path", "unmatched")
            elapsed = time.perf_counter() - start
            REQUESTS.labels(request.method, route_path, str(status)).inc()
            LATENCY.labels(request.method, route_path).observe(elapsed)
            level = logging.ERROR if status >= 500 else logging.INFO
            logger.log(
                level,
                f"{request.method} {route_path} -> {status}",
                extra={
                    "ctx_http_status": status,
                    "ctx_duration_ms": round(elapsed * 1000, 1),
                },
            )

    # `async def` on purpose: a plain `def` runs in FastAPI's thread pool, and
    # when that pool is exhausted (e.g. by hanging DNS lookups to a dead
    # service) Prometheus scrapes time out and the service wrongly looks down.
    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return logger


def get_tracer(service: str):
    return trace.get_tracer(service)
