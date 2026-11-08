"""Builds fake Tempo trace payloads (same JSON shape Tempo really returns)."""

import base64
import itertools

_ids = itertools.count(1)
T0 = 1_800_000_000_000_000_000  # an arbitrary start time in ns
MS = 1_000_000


def _b64(number: int, size: int) -> str:
    return base64.b64encode(number.to_bytes(size, "big")).decode()


def span(service, name, kind, start_ms, end_ms, parent=None, error=False,
         http_status=None, exception=None, **attributes):
    """Returns a dict describing one span; `parent` is another span dict."""
    attrs = dict(attributes)
    if http_status is not None:
        attrs["http.status_code"] = http_status
    return {
        "id": next(_ids), "service": service, "name": name, "kind": kind,
        "start": T0 + start_ms * MS, "end": T0 + end_ms * MS,
        "parent": parent["id"] if parent else None, "error": error,
        "attributes": attrs, "exception": exception,
    }


def tempo_payload(spans: list[dict]) -> dict:
    batches = {}
    for s in spans:
        raw = {
            "spanId": _b64(s["id"], 8),
            "name": s["name"],
            "kind": f"SPAN_KIND_{s['kind'].upper()}",
            "startTimeUnixNano": str(s["start"]),
            "endTimeUnixNano": str(s["end"]),
            "attributes": [
                {"key": k, "value": {"intValue": str(v)} if isinstance(v, int)
                 else {"stringValue": v}}
                for k, v in s["attributes"].items()
            ],
            "status": {"code": "STATUS_CODE_ERROR"} if s["error"] else {},
        }
        if s["parent"]:
            raw["parentSpanId"] = _b64(s["parent"], 8)
        if s["exception"]:
            exc_type, exc_message = s["exception"]
            raw["events"] = [{"name": "exception", "attributes": [
                {"key": "exception.type", "value": {"stringValue": exc_type}},
                {"key": "exception.message", "value": {"stringValue": exc_message}},
            ]}]
        batches.setdefault(s["service"], []).append(raw)
    return {"batches": [
        {"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": service}}]},
         "scopeSpans": [{"spans": raws}]}
        for service, raws in batches.items()
    ]}


DB_ATTRS = {"db.system": "postgresql", "peer.service": "postgresql"}


def db_down_trace():
    """inventory cannot connect to PostgreSQL -> 503 -> 502 -> 502."""
    gw = span("gateway", "GET /orders/check/{product_id}", "server", 0, 2100,
              error=True, http_status=502)
    gw_c = span("gateway", "GET", "client", 2, 2098, gw, error=True, http_status=502,
                **{"http.url": "http://orders:8002/orders/check/101"})
    od = span("orders", "GET /orders/check/{product_id}", "server", 4, 2096, gw_c,
              error=True, http_status=502)
    od_c = span("orders", "GET", "client", 5, 2094, od, error=True, http_status=503,
                **{"http.url": "http://inventory:8001/inventory/101"})
    inv = span("inventory", "GET /inventory/{product_id}", "server", 6, 2092, od_c,
               error=True, http_status=503)
    db = span("inventory", "postgresql connect", "client", 7, 2090, inv, error=True,
              exception=("psycopg.OperationalError", "connection timeout expired"),
              **DB_ATTRS)
    return tempo_payload([gw, gw_c, od, od_c, inv, db])


def inventory_unreachable_trace():
    """orders cannot connect to inventory at all -> 503 -> 503."""
    gw = span("gateway", "GET /orders/check/{product_id}", "server", 0, 20,
              error=True, http_status=503)
    gw_c = span("gateway", "GET", "client", 1, 19, gw, error=True, http_status=503,
                **{"http.url": "http://orders:8002/orders/check/101"})
    od = span("orders", "GET /orders/check/{product_id}", "server", 2, 18, gw_c,
              error=True, http_status=503)
    od_c = span("orders", "GET", "client", 3, 15, od, error=True,
                exception=("httpx.ConnectError", "Name or service not known"),
                **{"http.url": "http://inventory:8001/inventory/101"})
    return tempo_payload([gw, gw_c, od, od_c])


def timeout_trace():
    """inventory's DB query takes 5s; orders gives up at 4s -> 504."""
    gw = span("gateway", "GET /orders/check/{product_id}", "server", 0, 4010,
              error=True, http_status=504)
    gw_c = span("gateway", "GET", "client", 1, 4008, gw, error=True, http_status=504,
                **{"http.url": "http://orders:8002/orders/check/101"})
    od = span("orders", "GET /orders/check/{product_id}", "server", 2, 4006, gw_c,
              error=True, http_status=504)
    od_c = span("orders", "GET", "client", 3, 4003, od, error=True,
                exception=("httpx.ReadTimeout", "timed out"),
                **{"http.url": "http://inventory:8001/inventory/101"})
    inv = span("inventory", "GET /inventory/{product_id}", "server", 5, 5020, od_c,
               http_status=200)
    conn = span("inventory", "postgresql connect", "client", 6, 10, inv, **DB_ATTRS)
    sleep = span("inventory", "SELECT pg_sleep", "client", 11, 5011, inv, **DB_ATTRS)
    return tempo_payload([gw, gw_c, od, od_c, inv, conn, sleep])


def slow_ok_trace():
    """Everything succeeds but the DB query takes 1.5s."""
    gw = span("gateway", "GET /orders/check/{product_id}", "server", 0, 1530,
              http_status=200)
    gw_c = span("gateway", "GET", "client", 1, 1528, gw, http_status=200,
                **{"http.url": "http://orders:8002/orders/check/101"})
    od = span("orders", "GET /orders/check/{product_id}", "server", 2, 1526, gw_c,
              http_status=200)
    od_c = span("orders", "GET", "client", 3, 1524, od, http_status=200,
                **{"http.url": "http://inventory:8001/inventory/101"})
    inv = span("inventory", "GET /inventory/{product_id}", "server", 4, 1522, od_c,
               http_status=200)
    sleep = span("inventory", "SELECT pg_sleep", "client", 6, 1508, inv, **DB_ATTRS)
    return tempo_payload([gw, gw_c, od, od_c, inv, sleep])


def not_found_trace():
    """A 404 for a missing product: client errors are not incidents."""
    gw = span("gateway", "GET /orders/check/{product_id}", "server", 0, 15,
              http_status=404)
    od_c = span("orders", "GET", "client", 3, 12, gw, error=True, http_status=404,
                **{"http.url": "http://inventory:8001/inventory/999"})
    return tempo_payload([gw, od_c])
