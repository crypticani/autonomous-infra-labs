"""Day 28. What these assert is that the counters move *and carry the right labels* --
a metric incremented under the wrong label name is not a smaller bug than one that never
fires, it is a bigger one, because the graph exists and reads zero.

Everything here goes through the endpoint rather than calling metrics.* directly. A test
that increments a counter and then reads it back tests prometheus_client; the question
worth asking is whether app.py's own paths reach it.
"""

import pytest
from conftest import metric
from fastapi.testclient import TestClient

import app as app_module
import metrics
from app import app
from errors import TriageProviderError
from test_app import AUTH, ENVELOPE, TOKEN, _fake_triage


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "TOKENS", {TOKEN})
    monkeypatch.setattr(app_module, "_runs", {})
    monkeypatch.setattr(app_module, "_starts", {})
    return TestClient(app)


REPO = ENVELOPE["repo"]


def test_a_completed_run_records_volume_verdict_and_priorities(client, monkeypatch):
    monkeypatch.setattr(app_module, "triage_findings", _fake_triage("critical"))
    before = {
        "accepted": metric("st_runs_total", repo=REPO, outcome="accepted"),
        "done": metric("st_runs_total", repo=REPO, outcome="done"),
        "raw": metric("st_findings_total", repo=REPO, stage="raw"),
        "triaged": metric("st_findings_total", repo=REPO, stage="triaged"),
        "fail": metric("st_verdicts_total", repo=REPO, verdict="fail"),
        "critical": metric("st_priorities_total", priority="critical"),
        # The zero rows matter as much as the non-zero ones: risk.assess tallies every
        # priority including the ones nothing landed on, and incrementing by 0 must not
        # be mistaken for incrementing by 1.
        "low": metric("st_priorities_total", priority="low"),
    }

    client.post("/triage", json={**ENVELOPE, "risk_threshold": 40}, headers=AUTH)

    assert (
        metric("st_runs_total", repo=REPO, outcome="accepted") == before["accepted"] + 1
    )
    assert metric("st_runs_total", repo=REPO, outcome="done") == before["done"] + 1
    assert metric("st_findings_total", repo=REPO, stage="raw") == before["raw"] + 1
    assert (
        metric("st_findings_total", repo=REPO, stage="triaged") == before["triaged"] + 1
    )
    assert metric("st_verdicts_total", repo=REPO, verdict="fail") == before["fail"] + 1
    assert metric("st_priorities_total", priority="critical") == before["critical"] + 1
    assert metric("st_priorities_total", priority="low") == before["low"]


def test_a_failed_run_is_counted_as_failed_and_still_timed(client, monkeypatch):
    """The run that costs the most is the one that dies late, so `failed` has to reach
    both the outcome counter and the duration histogram -- see the `finally` in app.py.
    """

    def exploding(findings, provider=None, batch_size=None):
        raise TriageProviderError("the model took too long", 504, provider="ollama")

    monkeypatch.setattr(app_module, "triage_findings", exploding)
    before_failed = metric("st_runs_total", repo=REPO, outcome="failed")
    before_timed = metric("st_run_duration_seconds_count")

    client.post("/triage", json=ENVELOPE, headers=AUTH)

    assert metric("st_runs_total", repo=REPO, outcome="failed") == before_failed + 1
    assert metric("st_run_duration_seconds_count") == before_timed + 1


@pytest.mark.parametrize(
    "reason,send",
    [
        ("auth", lambda c: c.post("/triage", json=ENVELOPE)),
        (
            "body_size",
            lambda c: c.post(
                "/triage",
                content=b"{}",
                headers={**AUTH, "Content-Length": "999999999"},
            ),
        ),
    ],
)
def test_every_control_that_refuses_says_which_one_did(client, reason, send):
    before = metric("st_refusals_total", reason=reason)
    send(client)
    assert metric("st_refusals_total", reason=reason) == before + 1


def test_the_rate_limit_refusal_is_counted(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_RUNS_PER_HOUR", 1)
    monkeypatch.setattr(app_module, "triage_findings", _fake_triage("low"))
    before = metric("st_refusals_total", reason="rate_limit")

    client.post("/triage", json=ENVELOPE, headers=AUTH)
    second = client.post("/triage", json=ENVELOPE, headers=AUTH)

    assert second.status_code == 429
    assert metric("st_refusals_total", reason="rate_limit") == before + 1


def test_the_repo_label_is_bounded_so_a_caller_cannot_grow_the_registry(monkeypatch):
    """The one label in this service whose values arrive in a request body. Without the
    cap a single valid token can mint a new time series per request until the process
    dies, and every scrape carries the whole set along the way.
    """
    monkeypatch.setattr(metrics, "MAX_REPO_LABELS", 2)

    assert metrics.repo_label("repo-a") == "repo-a"
    assert metrics.repo_label("repo-b") == "repo-b"
    assert metrics.repo_label("repo-c") == "other"
    # Already-seen repos keep their own series after the cap is reached -- the fold is
    # for new arrivals, not a switch that turns per-repo graphs off for everyone.
    assert metrics.repo_label("repo-a") == "repo-a"


def test_metrics_endpoint_serves_the_exposition_without_a_token(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    # The name, not the value: this asserts the registry the endpoint serves is the one
    # this module declared into, which is the part that silently breaks.
    assert "st_runs_total" in response.text
