from datetime import datetime, timedelta, timezone

from tests import trace_builder as tb
from tests.fakes import FakeLoki, FakePrometheus, FakeTempo
from traceops.investigator import Investigator

SERVICES = {
    "gateway": "http://gateway:8003",
    "orders": "http://orders:8002",
    "inventory": "http://inventory:8001",
}
END = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
START = END - timedelta(minutes=5)

DB_DOWN_METRICS = {
    'status=~"5.."}[300s])) /': {"gateway": 1.0, "orders": 1.0, "inventory": 1.0},
    "sum by (job) (increase(http_requests_total[300s]))": {
        "gateway": 40, "orders": 40, "inventory": 40},
    "up{": {"gateway": 0, "orders": 0, "inventory": 0},  # failed scrape counts
}


def investigator(prometheus, tempo, loki):
    return Investigator(prometheus, tempo, loki, SERVICES)


def test_database_outage_is_traced_to_postgresql():
    report = investigator(
        FakePrometheus(DB_DOWN_METRICS),
        FakeTempo({f"t{i}": tb.db_down_trace() for i in range(5)}),
        FakeLoki({"inventory": ["Inventory database unavailable"] * 5}),
    ).investigate(START, END)

    # Observations: every service shows errors.
    assert {s.service for s in report.symptoms if s.signal == "error_rate"} == {
        "gateway", "orders", "inventory"}

    # Inference: the origin is the database, seen from inventory.
    top = report.hypotheses[0]
    assert top.service == "postgresql"
    assert top.failure_type == "dependency_unavailable"
    assert top.confidence_label == "high"  # 5/5 traces + logs + metrics
    assert report.propagation[-1].is_origin
    assert report.propagation[-1].operation == "postgresql connect"
    assert "inventory (HTTP 503) -> orders (HTTP 502) -> gateway (HTTP 502)" in report.summary

    # Every evidence id cited by a hypothesis exists.
    ids = {e.id for e in report.evidence}
    assert set(top.evidence_ids) <= ids


def test_unreachable_inventory_uses_scrape_failures_as_corroboration():
    metrics = dict(DB_DOWN_METRICS)
    metrics["up{"] = {"gateway": 0, "orders": 0, "inventory": 12}
    report = investigator(
        FakePrometheus(metrics),
        FakeTempo({"t1": tb.inventory_unreachable_trace()}),
        FakeLoki({"orders": ["Cannot reach Inventory Service"]}),
    ).investigate(START, END)

    top = report.hypotheses[0]
    assert (top.service, top.failure_type) == ("inventory", "service_unreachable")
    assert top.confidence == 1.0
    assert any(s.signal == "availability" for s in report.symptoms)
    assert any("crashed process" in note for note in report.uncertainty)


def test_a_single_missed_scrape_is_not_an_outage():
    # Seen for real: restarting a container loses one scrape.
    metrics = dict(DB_DOWN_METRICS)
    metrics["up{"] = {"gateway": 1, "orders": 1, "inventory": 2}
    report = investigator(FakePrometheus(metrics), FakeTempo({}), FakeLoki()).investigate(
        START, END)
    assert not any(s.signal == "availability" for s in report.symptoms)


def test_incomplete_traces_are_ignored_for_slowness():
    payload = tb.slow_ok_trace()
    # Drop the gateway's spans, as if they had not reached Tempo yet.
    payload["batches"] = [b for b in payload["batches"]
                          if b["resource"]["attributes"][0]["value"]["stringValue"] != "gateway"]
    report = investigator(
        FakePrometheus({}), FakeTempo({"t1": payload}), FakeLoki()
    ).investigate(START, END)
    assert report.stats["slow_traces"] == 0


def test_timeout_is_attributed_to_the_slow_query_not_orders():
    report = investigator(
        FakePrometheus({}), FakeTempo({"t1": tb.timeout_trace()}), FakeLoki()
    ).investigate(START, END)
    top = report.hypotheses[0]
    assert top.failure_type == "slow_dependency"
    assert top.component.startswith("SELECT pg_sleep")
    assert len(report.hypotheses) == 1  # no duplicate "orders is slow" entries


def test_tempo_outage_falls_back_to_metrics_with_low_confidence():
    report = investigator(
        FakePrometheus(DB_DOWN_METRICS), FakeTempo(down=True), FakeLoki()
    ).investigate(START, END)
    assert report.data_sources["tempo"].startswith("unavailable")
    top = report.hypotheses[0]
    assert top.confidence_label == "low"
    assert top.service == "inventory"  # most downstream service with errors
    assert any("Tempo was unavailable" in note for note in report.uncertainty)


def test_healthy_window_produces_no_hypotheses():
    report = investigator(FakePrometheus({}), FakeTempo({}), FakeLoki()).investigate(
        START, END)
    assert report.hypotheses == []
    assert report.summary == "No abnormal behaviour was found in this time window."
