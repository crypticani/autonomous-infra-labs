"""The webhook's wiring -- Day 20.

The first tests in this service to go through FastAPI rather than call a module. That is
deliberate and narrow: everything worth asserting about *diagnosis* is already covered by
calling diagnose() directly, and what these cover is the part that only exists as routing
-- the status code Alertmanager sees, and the fact that a failure in the background never
becomes one.
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module
from errors import GuardrailViolation, UpstreamError
from test_alerts import an_alert, payload


@pytest.fixture
def client(monkeypatch):
    """Returns the client and the list of alerts that reached the diagnosis path.

    _diagnose_and_propose is replaced rather than the provider, because a test that
    stubbed the model would still run the real loop, the real guardrails and the real
    Slack post. What is under test here is which alerts get that far.
    """
    diagnosed: list[dict] = []
    monkeypatch.setattr(app_module, "_diagnose_and_propose", diagnosed.append)
    return TestClient(app_module.app), diagnosed


def test_a_firing_alert_is_accepted_and_diagnosed(client):
    http, diagnosed = client

    response = http.post("/alerts", json=payload(an_alert()))

    assert response.status_code == 202
    assert response.json() == {"accepted": 1, "resolved": 0, "duplicate": 0}
    assert len(diagnosed) == 1
    assert diagnosed[0]["labels"]["namespace"] == "sandbox"


def test_the_webhook_answers_with_counts_and_not_a_diagnosis(client):
    # 202, not 200: the diagnosis has been accepted, not performed. Alertmanager's
    # webhook client gives up after ten seconds and re-POSTs; a diagnosis runs for
    # minutes, so answering it synchronously guarantees a timeout and a duplicate.
    http, _ = client

    response = http.post("/alerts", json=payload(an_alert()))

    assert response.status_code == 202
    assert "summary" not in response.json()


def test_a_resolved_alert_costs_nothing(client):
    http, diagnosed = client

    response = http.post("/alerts", json=payload(an_alert(status="resolved")))

    assert response.status_code == 202
    assert response.json()["resolved"] == 1
    assert diagnosed == []


def test_the_same_alert_re_sent_is_not_diagnosed_twice(client):
    http, diagnosed = client
    http.post("/alerts", json=payload(an_alert()))

    response = http.post("/alerts", json=payload(an_alert()))

    assert response.json() == {"accepted": 0, "resolved": 0, "duplicate": 1}
    assert len(diagnosed) == 1


def test_a_guardrail_refusal_in_the_background_still_answers_202(client, monkeypatch):
    # The whole reason the background path has its own error handling. A non-2xx makes
    # Alertmanager retry, and retrying a guardrail refusal is a loop that ends when the
    # alert resolves -- hammering an endpoint that is deliberately saying no.
    def refuse(alert):
        raise GuardrailViolation("30 model calls in the last 60m", guard="llm_budget")

    monkeypatch.setattr(app_module, "_diagnose_and_propose", refuse)
    http, _ = client

    assert http.post("/alerts", json=payload(an_alert())).status_code == 202


def test_an_upstream_failure_in_the_background_still_answers_202(client, monkeypatch):
    def fail(alert):
        raise UpstreamError("gemini is down", 502, provider="gemini")

    monkeypatch.setattr(app_module, "_diagnose_and_propose", fail)
    http, _ = client

    assert http.post("/alerts", json=payload(an_alert())).status_code == 202


def test_an_unexpected_failure_in_the_background_still_answers_202(client, monkeypatch):
    # Not defensive noise: this runs with no request to fail and no human watching, so
    # the only alternative to catching everything is a traceback nobody reads and an
    # Alertmanager retry storm.
    def explode(alert):
        raise ZeroDivisionError("something nobody predicted")

    monkeypatch.setattr(app_module, "_diagnose_and_propose", explode)
    http, _ = client

    assert http.post("/alerts", json=payload(an_alert())).status_code == 202


def test_the_webhook_requires_the_bearer_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "SHA_API_TOKEN", "s3cret")
    http, diagnosed = client

    assert http.post("/alerts", json=payload(an_alert())).status_code == 401
    assert diagnosed == []
    assert (
        http.post(
            "/alerts",
            json=payload(an_alert()),
            headers={"Authorization": "Bearer s3cret"},
        ).status_code
        == 202
    )


def test_metrics_are_exposed_for_prometheus(client):
    http, _ = client
    http.post("/alerts", json=payload(an_alert()))

    response = http.get("/metrics")

    assert response.status_code == 200
    assert "sha_alerts_received_total" in response.text
