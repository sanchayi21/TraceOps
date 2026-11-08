"""Thin HTTP clients for the three telemetry backends.

Each method returns plain Python data and raises `TelemetryUnavailable` when
the backend cannot be reached, so the investigator can record "Tempo was
down" as uncertainty instead of crashing.
"""

from datetime import datetime

import httpx


class TelemetryUnavailable(Exception):
    pass


class _Client:
    name = "backend"

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            response = httpx.get(
                f"{self.base_url}{path}", params=params, timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise TelemetryUnavailable(f"{self.name}: {error}") from error


class PrometheusClient(_Client):
    name = "prometheus"

    def query(self, promql: str, at: datetime) -> dict[str, float]:
        """Instant query. Returns {job: value} for each series."""
        data = self._get(
            "/api/v1/query", {"query": promql, "time": at.timestamp()}
        )
        result = {}
        for series in data.get("data", {}).get("result", []):
            job = series["metric"].get("job", "all")
            try:
                value = float(series["value"][1])
            except (KeyError, ValueError):
                continue
            if value == value:  # skip NaN (0/0 when there was no traffic)
                result[job] = value
        return result


class TempoClient(_Client):
    name = "tempo"

    def search(self, traceql: str, start: datetime, end: datetime, limit: int = 20):
        """Returns a list of trace IDs matching a TraceQL query."""
        data = self._get(
            "/api/search",
            {
                "q": traceql,
                "start": int(start.timestamp()),
                "end": int(end.timestamp()) + 1,
                "limit": limit,
            },
        )
        # Tempo's search API drops leading zeros from trace IDs, but logs carry
        # the full 32-hex-character form; pad so the two can be joined.
        return [trace["traceID"].zfill(32) for trace in data.get("traces", [])]

    def get_trace(self, trace_id: str) -> dict:
        return self._get(f"/api/traces/{trace_id}")


class LokiClient(_Client):
    name = "loki"

    def query_range(
        self, logql: str, start: datetime, end: datetime, limit: int = 50
    ) -> list[dict]:
        """Returns log entries, newest first: {timestamp, service, level, message, trace_id}."""
        data = self._get(
            "/loki/api/v1/query_range",
            {
                "query": logql,
                "start": int(start.timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9),
                "limit": limit,
                "direction": "backward",
            },
        )
        entries = []
        for stream in data.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for timestamp_ns, line in stream.get("values", []):
                entries.append(
                    {
                        "timestamp_ns": int(timestamp_ns),
                        "service": labels.get("service_name"),
                        "level": labels.get("severity_text")
                        or labels.get("detected_level"),
                        "message": line,
                        "trace_id": labels.get("trace_id"),
                        "span_id": labels.get("span_id"),
                    }
                )
        entries.sort(key=lambda entry: entry["timestamp_ns"], reverse=True)
        return entries[:limit]
