"""In-memory stand-ins for the telemetry backends and the incident store."""

import itertools
from datetime import datetime, timezone

from traceops.models import Incident, IncidentSummary
from traceops.telemetry_clients import TelemetryUnavailable


class FakePrometheus:
    def __init__(self, results: dict[str, dict[str, float]] | None = None, down=False):
        self.results = results or {}
        self.down = down

    def query(self, promql, at):
        if self.down:
            raise TelemetryUnavailable("prometheus: connection refused")
        for marker, result in self.results.items():
            if marker in promql and ("offset" in promql) == ("offset" in marker):
                return result
        return {}


class FakeTempo:
    def __init__(self, traces: dict[str, dict] | None = None, down=False):
        self.traces = traces or {}
        self.down = down

    def search(self, traceql, start, end, limit=20):
        if self.down:
            raise TelemetryUnavailable("tempo: connection refused")
        return list(self.traces)[:limit]

    def get_trace(self, trace_id):
        return self.traces[trace_id]


class FakeLoki:
    def __init__(self, entries_by_service: dict[str, list[str]] | None = None):
        self.entries = entries_by_service or {}

    def query_range(self, logql, start, end, limit=50):
        for service, messages in self.entries.items():
            if f'service_name="{service}"' in logql:
                return [
                    {"timestamp_ns": i, "service": service, "level": "ERROR",
                     "message": m, "trace_id": None, "span_id": None}
                    for i, m in enumerate(messages)
                ]
        return []


class InMemoryStore:
    def __init__(self):
        self.rows: dict[int, Incident] = {}
        self._ids = itertools.count(1)

    def init_schema(self):
        pass

    def create(self, **fields) -> Incident:
        now = datetime.now(timezone.utc)
        incident = Incident(
            id=next(self._ids), status="open", investigation_status="pending",
            created_at=now, updated_at=now, **fields,
        )
        self.rows[incident.id] = incident
        return incident

    def get(self, incident_id):
        return self.rows.get(incident_id)

    def list(self, limit=50):
        return [IncidentSummary(**i.model_dump()) for i in self.rows.values()]

    def update(self, incident_id, **fields):
        incident = self.rows[incident_id].model_copy(update=fields)
        self.rows[incident_id] = Incident.model_validate(incident.model_dump())
        return self.rows[incident_id]

    def find_open_alert_incident(self, since):
        candidates = [
            i for i in self.rows.values()
            if i.source == "alertmanager" and i.status == "open" and i.created_at >= since
        ]
        return candidates[-1] if candidates else None
