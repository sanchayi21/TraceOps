from datetime import datetime, timezone

import httpx
import pytest

from traceops.telemetry_clients import TelemetryUnavailable, TempoClient

AT = datetime(2026, 11, 7, tzinfo=timezone.utc)


def test_tempo_trace_ids_are_padded_to_32_hex_chars(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return httpx.Response(
            200, json={"traces": [{"traceID": "12dc98c4c63932bd603ad00baec3bef"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    ids = TempoClient("http://tempo:3200").search("{ status = error }", AT, AT)
    assert ids == ["012dc98c4c63932bd603ad00baec3bef"]


def test_unreachable_backend_raises_telemetry_unavailable():
    client = TempoClient("http://127.0.0.1:1", timeout=0.5)
    with pytest.raises(TelemetryUnavailable):
        client.search("{ status = error }", AT, AT)
