import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from traceops import alerts as alert_handling
from traceops.config import settings
from traceops.demo import router as demo_router
from traceops.explain import ExplanationUnavailable, explain_with_llm
from traceops.investigator import Investigator
from traceops.models import Incident, IncidentSummary, ManualInvestigationRequest
from traceops.store import IncidentStore
from traceops.telemetry_clients import LokiClient, PrometheusClient, TempoClient

logger = logging.getLogger("traceops")
logging.basicConfig(level=logging.INFO)

# Webhooks can arrive concurrently; serialise the "find or create incident"
# step so two alerts for the same failure can't both open a new incident.
_webhook_lock = threading.Lock()


def get_store() -> IncidentStore:
    return IncidentStore(settings)


def get_investigator() -> Investigator:
    return Investigator(
        PrometheusClient(settings.prometheus_url),
        TempoClient(settings.tempo_url),
        LokiClient(settings.loki_url),
        settings.services,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        get_store().init_schema()
    except psycopg.OperationalError:
        logger.warning("TraceOps database unavailable at startup; will retry on use")
    yield


app = FastAPI(title="TraceOps", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(demo_router)


@app.get("/health")  # door we knock on and backend answers if it's running
def health_check():
    return {"status": "ok", "service": "traceops"}


def run_investigation(incident_id: int, store: IncidentStore, investigator: Investigator):
    """Runs after the HTTP response is sent (FastAPI BackgroundTasks)."""
    incident = store.get(incident_id)
    if incident is None:
        return
    store.update(incident_id, investigation_status="running")
    end = datetime.now(timezone.utc)
    try:
        report = investigator.investigate(incident.window_start, end)
    except Exception as error:  # record the failure on the incident itself
        logger.exception("Investigation %s failed", incident_id)
        store.update(incident_id, investigation_status="failed", error=str(error))
        return
    services = set(incident.services) | {s.service for s in report.symptoms}
    if report.hypotheses:
        services.add(report.hypotheses[0].service)
    store.update(
        incident_id,
        report=report,
        investigation_status="completed",
        window_end=end,
        services=sorted(services),
        error=None,
    )


# ------------------------------------------------------------------ incidents


@app.get("/api/incidents", response_model=list[IncidentSummary])
def list_incidents(store: IncidentStore = Depends(get_store)):
    return store.list()


@app.get("/api/incidents/{incident_id}", response_model=Incident)
def get_incident(incident_id: int, store: IncidentStore = Depends(get_store)):
    incident = store.get(incident_id)
    if incident is None:
        raise HTTPException(404, "Incident not found")
    return incident


@app.post("/api/investigations", response_model=Incident, status_code=202)
def start_manual_investigation(
    request: ManualInvestigationRequest,
    background: BackgroundTasks,
    store: IncidentStore = Depends(get_store),
    investigator: Investigator = Depends(get_investigator),
):
    now = datetime.now(timezone.utc)
    scope = request.service or "all services"
    incident = store.create(
        title=request.title or f"Manual investigation: {scope}, last {request.minutes} min",
        severity="info",
        source="manual",
        services=[request.service] if request.service else [],
        window_start=now - timedelta(minutes=request.minutes),
        window_end=now,
        alerts=[],
    )
    background.add_task(run_investigation, incident.id, store, investigator)
    return incident


@app.post("/api/incidents/{incident_id}/reinvestigate", response_model=Incident,
          status_code=202)
def reinvestigate(
    incident_id: int,
    background: BackgroundTasks,
    store: IncidentStore = Depends(get_store),
    investigator: Investigator = Depends(get_investigator),
):
    incident = get_incident(incident_id, store)
    background.add_task(run_investigation, incident.id, store, investigator)
    return store.update(incident.id, investigation_status="pending")


@app.post("/api/incidents/{incident_id}/resolve", response_model=Incident)
def resolve(incident_id: int, store: IncidentStore = Depends(get_store)):
    get_incident(incident_id, store)
    return store.update(
        incident_id, status="resolved", resolved_at=datetime.now(timezone.utc)
    )


@app.post("/api/incidents/{incident_id}/explain", response_model=Incident)
def explain(incident_id: int, store: IncidentStore = Depends(get_store)):
    incident = get_incident(incident_id, store)
    if incident.report is None:
        raise HTTPException(409, "Investigation has not finished yet")
    try:
        explanation = explain_with_llm(incident.report)
    except ExplanationUnavailable as error:
        raise HTTPException(503, str(error))
    return store.update(incident_id, explanation=explanation)


@app.get("/api/config")
def public_config():
    return {
        "explain_provider": settings.explain_provider,
        "services": list(settings.services),
        "grafana_url": "http://localhost:3000",
    }


# ------------------------------------------------------------------ alerts


@app.post("/api/alerts/alertmanager")
def alertmanager_webhook(
    payload: dict,
    background: BackgroundTasks,
    store: IncidentStore = Depends(get_store),
    investigator: Investigator = Depends(get_investigator),
):
    with _webhook_lock:
        incident, should_investigate = alert_handling.handle_webhook(
            payload, store, datetime.now(timezone.utc)
        )
    if incident and should_investigate:
        background.add_task(run_investigation, incident.id, store, investigator)
    return {"incident_id": incident.id if incident else None,
            "investigating": bool(incident and should_investigate)}
