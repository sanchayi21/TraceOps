from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main
from tests import trace_builder as tb
from tests.fakes import FakeLoki, FakePrometheus, FakeTempo, InMemoryStore
from tests.test_investigator import DB_DOWN_METRICS, SERVICES
from traceops.investigator import Investigator


def alert(name, job, status="firing", fingerprint=None):
    starts = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    return {
        "status": status,
        "labels": {"alertname": name, "job": job, "severity": "critical"},
        "annotations": {"summary": f"{job} is failing"},
        "startsAt": starts,
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": fingerprint or f"{name}-{job}",
    }


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def client(store, monkeypatch):
    investigator = Investigator(
        FakePrometheus(DB_DOWN_METRICS),
        FakeTempo({"t1": tb.db_down_trace()}),
        FakeLoki({"inventory": ["Inventory database unavailable"]}),
        SERVICES,
    )
    main.app.dependency_overrides[main.get_store] = lambda: store
    main.app.dependency_overrides[main.get_investigator] = lambda: investigator
    monkeypatch.setattr(main, "get_store", lambda: store)  # used by lifespan
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


def test_alert_webhook_creates_and_investigates_incident(client):
    response = client.post("/api/alerts/alertmanager", json={
        "status": "firing",
        "alerts": [alert("HighErrorRate", "gateway"), alert("HighErrorRate", "inventory")],
    })
    assert response.json() == {"incident_id": 1, "investigating": True}

    incident = client.get("/api/incidents/1").json()
    assert incident["title"] == "HighErrorRate on gateway, inventory"
    assert incident["investigation_status"] == "completed"
    assert incident["report"]["hypotheses"][0]["service"] == "postgresql"
    assert "postgresql" in incident["services"]


def test_related_alerts_merge_into_one_incident(client):
    client.post("/api/alerts/alertmanager", json={"alerts": [alert("HighErrorRate", "gateway")]})
    second = client.post("/api/alerts/alertmanager", json={"alerts": [alert("HighLatency", "orders")]})
    assert second.json()["incident_id"] == 1
    assert len(client.get("/api/incidents").json()) == 1

    # A repeat of an already-firing alert does not trigger another investigation.
    repeat = client.post("/api/alerts/alertmanager", json={"alerts": [alert("HighLatency", "orders")]})
    assert repeat.json()["investigating"] is False


def test_incident_resolves_when_all_alerts_resolve(client):
    client.post("/api/alerts/alertmanager", json={"alerts": [alert("HighErrorRate", "gateway")]})
    client.post("/api/alerts/alertmanager", json={
        "alerts": [alert("HighErrorRate", "gateway", status="resolved")]})
    incident = client.get("/api/incidents/1").json()
    assert incident["status"] == "resolved"
    assert incident["resolved_at"] is not None


def test_manual_investigation(client):
    response = client.post("/api/investigations", json={"minutes": 5})
    assert response.status_code == 202
    incident = client.get(f"/api/incidents/{response.json()['id']}").json()
    assert incident["source"] == "manual"
    assert incident["report"]["stats"]["failed_traces"] == 1


def test_explain_without_llm_returns_clear_error(client):
    incident_id = client.post("/api/investigations", json={"minutes": 5}).json()["id"]
    response = client.post(f"/api/incidents/{incident_id}/explain")
    assert response.status_code == 503
    assert "No LLM is configured" in response.json()["detail"]


def test_unknown_incident_is_404(client):
    assert client.get("/api/incidents/999").status_code == 404
