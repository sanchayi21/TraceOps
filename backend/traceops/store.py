"""Incident storage in TraceOps' own PostgreSQL database.

Queryable fields (status, severity, services, times) are real columns. The
investigation report is nested and evolves with the investigator, so it is
stored as JSONB and validated by the pydantic models on the way in and out.
"""

from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from traceops.config import Settings
from traceops.models import Incident, IncidentSummary

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id                   SERIAL PRIMARY KEY,
    title                TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'open',
    investigation_status TEXT NOT NULL DEFAULT 'pending',
    severity             TEXT NOT NULL DEFAULT 'warning',
    source               TEXT NOT NULL,
    services             TEXT[] NOT NULL DEFAULT '{}',
    window_start         TIMESTAMPTZ NOT NULL,
    window_end           TIMESTAMPTZ NOT NULL,
    alerts               JSONB NOT NULL DEFAULT '[]',
    report               JSONB,
    explanation          JSONB,
    error                TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at          TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS incidents_created_at_idx ON incidents (created_at DESC);
"""

UPDATABLE = {
    "title", "status", "investigation_status", "severity", "services",
    "window_start", "window_end", "alerts", "report", "explanation", "error",
    "resolved_at",
}
JSON_COLUMNS = {"alerts", "report", "explanation"}


def _to_db(column: str, value):
    if column not in JSON_COLUMNS or value is None:
        return value
    if isinstance(value, BaseModel):
        return Jsonb(value.model_dump(mode="json"))
    if isinstance(value, list):
        return Jsonb([
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            for item in value
        ])
    return Jsonb(value)


class IncidentStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _connect(self):
        return psycopg.connect(
            host=self.settings.db_host,
            port=self.settings.db_port,
            dbname=self.settings.db_name,
            user=self.settings.db_user,
            password=self.settings.db_password,
            connect_timeout=3,
            row_factory=dict_row,
        )

    def init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(SCHEMA)

    def create(self, *, title, severity, source, services, window_start,
               window_end, alerts) -> Incident:
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO incidents
                    (title, severity, source, services, window_start, window_end, alerts)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (title, severity, source, services, window_start, window_end,
                 _to_db("alerts", alerts)),
            ).fetchone()
        return Incident.model_validate(row)

    def get(self, incident_id: int) -> Incident | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE id = %s", (incident_id,)
            ).fetchone()
        return Incident.model_validate(row) if row else None

    def list(self, limit: int = 50) -> list[IncidentSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, status, investigation_status, severity, source,
                       services, created_at, resolved_at,
                       report -> 'hypotheses' -> 0 ->> 'component' AS top_hypothesis,
                       report -> 'hypotheses' -> 0 ->> 'confidence_label' AS confidence_label,
                       report -> 'hypotheses' -> 0 ->> 'service' AS top_service,
                       report -> 'hypotheses' -> 0 ->> 'failure_type' AS top_failure_type,
                       (report -> 'hypotheses' -> 0 ->> 'confidence')::float AS top_confidence,
                       COALESCE(report -> 'symptoms', '[]'::jsonb) AS symptoms
                FROM incidents ORDER BY created_at DESC LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [IncidentSummary.model_validate(row) for row in rows]

    def update(self, incident_id: int, **fields) -> Incident:
        unknown = set(fields) - UPDATABLE
        if unknown:
            raise ValueError(f"Cannot update {unknown}")
        assignments = ", ".join(f"{column} = %s" for column in fields)
        values = [_to_db(column, value) for column, value in fields.items()]
        with self._connect() as connection:
            row = connection.execute(
                f"UPDATE incidents SET {assignments}, updated_at = now() "
                "WHERE id = %s RETURNING *",
                (*values, incident_id),
            ).fetchone()
        return Incident.model_validate(row)

    def find_open_alert_incident(self, since: datetime) -> Incident | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM incidents
                WHERE source = 'alertmanager' AND status = 'open' AND created_at >= %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (since,),
            ).fetchone()
        return Incident.model_validate(row) if row else None
