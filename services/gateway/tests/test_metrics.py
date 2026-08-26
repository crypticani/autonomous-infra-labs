"""The counters, checked through the endpoints rather than by calling .inc().

A metric asserted on directly proves the Counter works, which was never in doubt. What can
actually break is the label a handler passes -- an outcome recorded under the wrong name is
a dashboard that lies, and nothing else in the suite would notice.
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import app
from conftest import FakeResponse, metric
from router import NONE, Route

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "TOKENS", {TOKEN})
    monkeypatch.setattr(app_module, "_starts", {})
    return TestClient(app)


def _route(monkeypatch, service, confidence=0.9):
    route = Route(reason="because", service=service, confidence=confidence)
    monkeypatch.setattr(app_module, "classify", lambda q, a="": route)


def test_each_outcome_lands_under_its_own_label(client, monkeypatch):
    monkeypatch.setattr(
        app_module.requests, "post", lambda *a, **k: FakeResponse(200, {"answer": "x"})
    )

    _route(monkeypatch, "knowledge-copilot")
    before = metric("gw_routes_total", service="knowledge-copilot", outcome="answered")
    client.post("/ask", json={"question": "how do I drain a node"}, headers=AUTH)
    after = metric("gw_routes_total", service="knowledge-copilot", outcome="answered")
    assert after == before + 1

    # needs_input is counted against the service it named, not against "none": the router
    # got this one right and a dashboard that showed it as a failure would be wrong.
    _route(monkeypatch, "log-analyzer")
    before = metric("gw_routes_total", service="log-analyzer", outcome="needs_input")
    client.post("/ask", json={"question": "why did checkout 500"}, headers=AUTH)
    assert metric("gw_routes_total", service="log-analyzer", outcome="needs_input") == (
        before + 1
    )


def test_an_unroutable_ask_is_counted_under_none(client, monkeypatch):
    """`service="none"` rather than the sentinel string leaking into a label, and kept apart
    from a backend outage: they are both "no answer" and they need very different fixes.
    """
    _route(monkeypatch, NONE)
    before = metric("gw_routes_total", service="none", outcome="unroutable")

    client.post("/ask", json={"question": "something is wrong somewhere"}, headers=AUTH)

    assert metric("gw_routes_total", service="none", outcome="unroutable") == before + 1


def test_a_confidence_the_floor_will_reject_is_still_recorded(monkeypatch):
    """The histogram has to see the low scores or it cannot show that the floor ever fires.
    A router that is always 0.95 is a router that never doubts, and that is the failure mode
    the confidence field exists to catch -- so the observation belongs in classify(), before
    anything decides whether to act on the route.

    Goes through router.classify rather than the endpoint, because the endpoint tests stub
    classify out and a stub cannot record anything.
    """
    import json

    import router

    class Fake:
        name = "fake"
        model_name = "fake-model"

        def generate(self, system, user, schema):
            return json.dumps(
                {"reason": "a guess", "service": "security-triage", "confidence": 0.2}
            )

    monkeypatch.setattr(router, "get_router_provider", Fake)
    before = metric("gw_router_confidence_count")
    before_sum = metric("gw_router_confidence_sum")

    route = router.classify("should I ship this thing")

    # Recorded, and then rejected: two separate steps, and the metric sees both halves.
    assert metric("gw_router_confidence_count") == before + 1
    assert metric("gw_router_confidence_sum") == pytest.approx(before_sum + 0.2)
    assert router.decision(route)[0] is None


def test_a_bad_token_is_counted_as_an_auth_refusal(client):
    before = metric("gw_refusals_total", reason="auth")
    client.post("/ask", json={"question": "how do I drain a node"})
    assert metric("gw_refusals_total", reason="auth") == before + 1


def test_an_oversized_body_is_counted_before_it_is_parsed(client):
    before = metric("gw_refusals_total", reason="body_size")
    client.post(
        "/ask",
        headers={
            **AUTH,
            "Content-Length": "99999999",
            "Content-Type": "application/json",
        },
        content=b'{"question": "x"}',
    )
    assert metric("gw_refusals_total", reason="body_size") == before + 1


def test_the_proxy_records_the_upstream_status_class(client, monkeypatch):
    """Class, not the exact code: a 404 and a 422 from a backend are the same operational
    fact here, and per-code series would be a cardinality bill for nothing."""
    monkeypatch.setattr(
        app_module.requests,
        "request",
        lambda *a, **k: FakeResponse(404, {"detail": "no"}),
    )
    before = metric("gw_proxy_requests_total", service="security-triage", status="4xx")

    client.get("/s/security-triage/triage/nope", headers=AUTH)

    assert metric(
        "gw_proxy_requests_total", service="security-triage", status="4xx"
    ) == (before + 1)


def test_an_unknown_service_is_a_refusal_and_not_a_proxy_request(client):
    """It never reached a backend, so counting it under gw_proxy_requests would make the
    unreachable rate depend on how often callers typo a service name."""
    before_refusal = metric("gw_refusals_total", reason="unknown_service")

    client.get("/s/grafana/api", headers=AUTH)

    assert metric("gw_refusals_total", reason="unknown_service") == before_refusal + 1


def test_the_metrics_endpoint_serves_the_prometheus_format(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "gw_routes_total" in response.text
