from tests import trace_builder as tb
from traceops import trace_analysis as ta

HOSTS = {"gateway": "gateway", "orders": "orders", "inventory": "inventory"}


def parse(payload):
    return ta.parse_trace("abc", payload)


def test_parse_links_spans_into_one_tree():
    trace = parse(tb.db_down_trace())
    assert trace.root.service == "gateway"
    assert len(trace.spans) == 6
    assert {s.service for s in trace.spans} == {"gateway", "orders", "inventory"}


def test_database_failure_origin_is_the_db_span_not_the_gateway():
    trace = parse(tb.db_down_trace())
    assert trace.failed
    origins = ta.find_error_origins(trace)
    assert [o.name for o in origins] == ["postgresql connect"]

    point = ta.classify_origin(origins[0], HOSTS)
    assert point.service == "postgresql"
    assert point.failure_type == "dependency_unavailable"
    assert "OperationalError" in point.detail


def test_unreachable_service_is_blamed_on_the_callee():
    trace = parse(tb.inventory_unreachable_trace())
    origin = ta.find_error_origins(trace)[0]
    point = ta.classify_origin(origin, HOSTS)
    assert origin.service == "orders"  # the span that observed it
    assert point.service == "inventory"  # where the problem is
    assert point.failure_type == "service_unreachable"


def test_connect_timeout_means_unreachable_not_slow():
    # Seen for real: with the inventory container stopped, Docker DNS lookups
    # hung and httpx raised ConnectTimeout rather than ConnectError.
    trace = parse(tb.inventory_unreachable_trace())
    origin = ta.find_error_origins(trace)[0]
    origin.exception_type = "httpx.ConnectTimeout"
    assert ta.classify_origin(origin, HOSTS).failure_type == "service_unreachable"


def test_failure_points_group_even_when_error_text_differs():
    a = ta.FailurePoint("inventory", "inventory (HTTP)", "service_unreachable", "ConnectError")
    b = ta.FailurePoint("inventory", "inventory (HTTP)", "service_unreachable", "ConnectTimeout")
    assert a == b and len({a, b}) == 1


def test_host_port_mapping_resolves_local_urls():
    trace = parse(tb.inventory_unreachable_trace())
    origin = ta.find_error_origins(trace)[0]
    origin.attributes["http.url"] = "http://127.0.0.1:8001/inventory/101"
    point = ta.classify_origin(origin, {"127.0.0.1:8001": "inventory"})
    assert point.service == "inventory"


def test_timeout_bottleneck_is_found_inside_the_slow_callee():
    trace = parse(tb.timeout_trace())
    origin = ta.find_error_origins(trace)[0]
    assert ta.classify_origin(origin, HOSTS).failure_type == "timeout"
    bottleneck = ta.locate_bottleneck(origin)
    assert bottleneck.name == "SELECT pg_sleep"
    assert ta.self_time_ms(bottleneck) == 5000


def test_self_time_excludes_children():
    trace = parse(tb.slow_ok_trace())
    assert not trace.failed
    assert ta.slowest_span(trace.spans).name == "SELECT pg_sleep"
    assert ta.self_time_ms(trace.root) < 5


def test_404_is_not_counted_as_a_failed_trace():
    assert not parse(tb.not_found_trace()).failed


def test_propagation_path_runs_root_to_origin():
    trace = parse(tb.db_down_trace())
    origin = ta.find_error_origins(trace)[0]
    path = ta.path_to(trace, origin)
    assert [s.service for s in path] == [
        "gateway", "gateway", "orders", "orders", "inventory", "inventory"
    ]
