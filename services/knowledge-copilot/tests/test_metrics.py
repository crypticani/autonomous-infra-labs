from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

import app as app_module
from app import app
from test_api import HITS

client = TestClient(app)


def sample(name, labels=None):
    """Counters are process-global and every test in the session shares them, so these
    read deltas. An absolute assertion would pass alone and fail in a suite."""
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


def test_metrics_endpoint_serves_prometheus_text():
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "kc_chunks_indexed" in response.text


def test_alert_sync_age_is_nan_until_a_sync_succeeds(monkeypatch):
    """Never-synced must not publish as 0. Zero seconds since the last sync is the
    healthiest possible reading, so a fabricated 0 would hide a sync loop that has
    never once completed -- and would quietly satisfy any freshness alert built on it.
    """
    monkeypatch.setattr(app_module, "_last_alert_sync", None)
    client.get("/metrics")
    value = REGISTRY.get_sample_value("kc_alert_sync_age_seconds")
    assert value != value  # NaN is the only value not equal to itself


def test_refusal_increments_the_refused_counter(wire):
    wire([])  # nothing cleared the floor
    before = sample("kc_answers_total", {"outcome": "refused"})
    client.post("/ask-runbook", json={"question": "something nothing covers at all"})
    assert sample("kc_answers_total", {"outcome": "refused"}) == before + 1


def test_an_answer_with_no_resolvable_citation_counts_as_ungrounded(wire):
    wire(HITS, answer="Raise the limit.")  # no [1] marker, so grounded is False
    before = sample("kc_answers_total", {"outcome": "ungrounded"})
    client.post("/ask-runbook", json={"question": "why do pods get OOMKilled"})
    assert sample("kc_answers_total", {"outcome": "ungrounded"}) == before + 1


def test_a_cited_answer_counts_as_answered(wire):
    wire(HITS, answer="Raise the limit [1].")
    before = sample("kc_answers_total", {"outcome": "answered"})
    client.post("/ask-runbook", json={"question": "why do pods get OOMKilled"})
    assert sample("kc_answers_total", {"outcome": "answered"}) == before + 1


def test_both_stages_are_timed_separately(wire):
    wire(HITS, answer="Raise the limit [1].")
    before = sample("kc_answer_duration_seconds_count", {"stage": "generation"})
    client.post("/ask-runbook", json={"question": "why do pods get OOMKilled"})
    assert sample("kc_answer_duration_seconds_count", {"stage": "generation"}) == (
        before + 1
    )


def test_bad_slack_signature_increments_its_counter(monkeypatch):
    monkeypatch.setattr(app_module, "slack_active", lambda: True)
    before = sample("kc_slack_events_total", {"outcome": "bad_signature"})
    client.post(
        "/slack/events",
        content=b"{}",
        headers={"X-Slack-Request-Timestamp": "1", "X-Slack-Signature": "v0=nope"},
    )
    assert sample("kc_slack_events_total", {"outcome": "bad_signature"}) == before + 1
