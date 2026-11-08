"""Tests for the demo services. No database or network needed:
the database calls and downstream HTTP calls are replaced with fakes.

Run from the repo root:  backend/.venv/Scripts/python -m pytest services/tests
"""

from contextlib import contextmanager

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from services.inventory import database
from services.inventory import main as inventory
from services.orders import main as orders


# ------------------------------------------------------------------ inventory


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


@pytest.fixture
def inventory_client(monkeypatch):
    calls = {"ports": [], "queries": []}
    products = {101: {"product_id": 101, "name": "Keyboard", "quantity": 10}}

    @contextmanager
    def fake_connection(port_override=None):
        calls["ports"].append(port_override)
        if port_override == inventory.UNREACHABLE_DB_PORT:
            raise psycopg.OperationalError("connection refused")
        yield object()

    def fake_query(connection, sql, params, operation):
        calls["queries"].append(operation)
        return FakeResult(products.get(params[0]) if operation == "SELECT products" else None)

    monkeypatch.setattr(database, "get_connection", fake_connection)
    monkeypatch.setattr(database, "run_query", fake_query)
    monkeypatch.setattr(database, "init_schema", lambda: None)
    inventory.chaos.mode = "none"
    with TestClient(inventory.app) as client:
        yield client, calls
    inventory.chaos.mode = "none"


def test_inventory_returns_product(inventory_client):
    client, _ = inventory_client
    response = client.get("/inventory/101")
    assert response.status_code == 200
    assert response.json()["name"] == "Keyboard"


def test_inventory_unknown_product_is_404(inventory_client):
    client, _ = inventory_client
    assert client.get("/inventory/999").status_code == 404


def test_db_down_fault_uses_a_real_connection_failure(inventory_client):
    client, calls = inventory_client
    client.put("/chaos", json={"mode": "db_down"})
    response = client.get("/inventory/101")
    assert response.status_code == 503
    assert calls["ports"][-1] == inventory.UNREACHABLE_DB_PORT


def test_slow_db_fault_runs_a_sleep_query(inventory_client):
    client, calls = inventory_client
    client.put("/chaos", json={"mode": "slow_db", "latency_ms": 10})
    assert client.get("/inventory/101").status_code == 200
    assert calls["queries"] == ["SELECT pg_sleep", "SELECT products"]


def test_error_fault_and_clearing_it(inventory_client):
    client, _ = inventory_client
    client.put("/chaos", json={"mode": "error", "error_rate": 1.0})
    assert client.get("/inventory/101").status_code == 500
    client.delete("/chaos")
    assert client.get("/inventory/101").status_code == 200


def test_metrics_are_exposed(inventory_client):
    client, _ = inventory_client
    client.get("/inventory/101")
    body = client.get("/metrics").text
    assert 'http_requests_total{method="GET",route="/inventory/{product_id}",status="200"}' in body


# ------------------------------------------------------------------ orders


@pytest.fixture
def orders_with_inventory(monkeypatch):
    """Point orders' outgoing calls at a handler instead of the network."""
    def install(handler):
        real_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(orders.httpx, "AsyncClient", client_factory)
        return TestClient(orders.app)

    return install


def raise_(error):
    def handler(request):
        raise error("simulated", request=request)
    return handler


@pytest.mark.parametrize(
    "handler, status",
    [
        (lambda r: httpx.Response(200, json={"product_id": 101}), 200),
        (lambda r: httpx.Response(404, json={"detail": "x"}), 404),
        (lambda r: httpx.Response(503, json={"detail": "x"}), 502),
        (raise_(httpx.ConnectError), 503),
        (raise_(httpx.ConnectTimeout), 503),  # can't connect != slow
        (raise_(httpx.ReadTimeout), 504),
    ],
)
def test_orders_maps_inventory_failures_to_status_codes(orders_with_inventory, handler, status):
    client = orders_with_inventory(handler)
    assert client.get("/orders/check/101").status_code == status
