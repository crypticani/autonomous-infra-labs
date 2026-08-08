import pytest
import requests
from conftest import SpyLLM
from fastapi.testclient import TestClient

import app as app_module
from app import NOT_COVERED, app
from errors import UpstreamError
from retrieval import EmptyIndexError, Hit

client = TestClient(app)

HITS = [
    Hit(
        text="Raise the container memory limit and check for a leak.",
        source="oomkilled-pod.md",
        chunk_index=0,
        doc_type="runbook",
        score=0.781,
    ),
    Hit(
        text="Checkout was OOMKilled after the limit was left at 128Mi.",
        source="postmortem-2026-06-checkout-oom-outage.md",
        chunk_index=1,
        doc_type="postmortem",
        score=0.712,
    ),
]

OOM_QUESTION = {"question": "why do my pods get OOMKilled after a deploy"}
IAM_QUESTION = {"question": "how do I rotate an IAM access key"}


def test_grounded_answer_returns_sources(wire):
    spy = wire(HITS, answer="Raise the limit [1]. June's outage was the same [2].")
    response = client.post("/ask-runbook", json=OOM_QUESTION)

    assert response.status_code == 200
    body = response.json()
    assert body["grounded"] is True
    assert body["answer_source"] == "runbooks"
    assert [s["marker"] for s in body["sources"]] == [1, 2]
    assert body["sources"][0]["source"] == "oomkilled-pod.md"
    assert spy.calls == 1
    assert '<chunk id="1"' in spy.last_prompts[1]


def test_invented_citation_is_stripped_not_a_502(wire):
    wire(HITS, answer="Raise the limit [1]. Also restart the kubelet [7].")
    body = client.post("/ask-runbook", json=OOM_QUESTION).json()

    assert "[7]" not in body["answer"]
    assert body["grounded"] is False
    assert [s["marker"] for s in body["sources"]] == [1]


def test_a_shell_subscript_is_not_a_citation(wire):
    wire(HITS, answer="Raise the limit [1], then read ${limits[0]} in the manifest.")
    body = client.post("/ask-runbook", json=OOM_QUESTION).json()

    # The old regex read [0] as an invented citation and ungrounded a good answer.
    assert "${limits[0]}" in body["answer"]
    assert body["grounded"] is True
    assert [s["marker"] for s in body["sources"]] == [1]


def test_below_floor_refuses_and_makes_no_llm_call(wire):
    spy = wire([], answer="this must never be produced")
    body = client.post("/ask-runbook", json=IAM_QUESTION).json()

    assert body["answer"] == NOT_COVERED
    assert body["grounded"] is False
    assert (
        body["answer_source"] == "none"
    )  # tells a caller this is a refusal, not prose
    assert body["sources"] == []
    assert spy.calls == 0  # the assertion this endpoint exists for


def test_an_uncited_answer_is_ungrounded_but_still_from_the_runbooks(wire):
    wire(HITS, answer="Raise the limit. No citations offered.")
    body = client.post("/ask-runbook", json=OOM_QUESTION).json()

    # Same grounded/sources as a refusal; answer_source is what separates them.
    assert body["grounded"] is False
    assert body["sources"] == []
    assert body["answer_source"] == "runbooks"


def test_empty_index_is_503(wire, monkeypatch):
    wire(HITS)

    def boom(*args, **kwargs):
        raise EmptyIndexError("collection 'knowledge_ollama_512_64' is empty")

    monkeypatch.setattr(app_module, "retrieve", boom)
    assert client.post("/ask-runbook", json=OOM_QUESTION).status_code == 503


@pytest.mark.parametrize("status", [502, 503, 504])
def test_upstream_status_is_passed_through(wire, status):
    wire(HITS, error=UpstreamError("upstream said no", status))
    assert client.post("/ask-runbook", json=OOM_QUESTION).status_code == status


def test_short_question_is_422():
    assert client.post("/ask-runbook", json={"question": "help"}).status_code == 422


def test_k_above_ten_is_422():
    response = client.post("/ask-runbook", json={**OOM_QUESTION, "k": 99})
    assert response.status_code == 422


def test_health_reports_both_upstreams(monkeypatch, provider):
    class Collection:
        name = "knowledge_fake_512_64"

        def count(self):
            return 42

    monkeypatch.setattr(app_module, "get_llm_provider", lambda: SpyLLM())
    monkeypatch.setattr(app_module, "open_collection", lambda: (provider, Collection()))
    body = client.get("/health").json()

    assert body["status"] == "healthy"
    assert body["issues"] == []
    assert body["chunks_indexed"] == 42
    assert (body["provider"], body["model"]) == ("spy", "spy-model")
    assert body["embedding_provider"] == "fake"
    assert body["slack"] == "disabled"  # conftest forces it off


class FakeOllama(SpyLLM):
    name = "ollama"
    model_name = "qwen2.5:7b-instruct"
    base_url = "http://appsrv:11434"


class Collection:
    name = "knowledge_ollama_512_64"

    def count(self):
        return 68


def wire_health(monkeypatch, tags_response, provider=None):
    monkeypatch.setattr(app_module, "get_llm_provider", lambda: FakeOllama())
    monkeypatch.setattr(app_module, "open_collection", lambda: (provider, Collection()))
    monkeypatch.setattr(app_module.requests, "get", tags_response)


def test_health_is_healthy_when_the_model_is_pulled(monkeypatch, provider):
    class Tags:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "qwen2.5:7b-instruct"}]}

    wire_health(monkeypatch, lambda *a, **k: Tags(), provider)
    body = client.get("/health").json()

    assert body["status"] == "healthy"
    assert body["issues"] == []


def test_health_degrades_when_the_model_is_not_pulled(monkeypatch, provider):
    class Tags:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "qwen2.5-coder:3b"}]}

    wire_health(monkeypatch, lambda *a, **k: Tags(), provider)
    body = client.get("/health").json()

    # Constructing OllamaProvider does no I/O, so this is the only place a missing
    # model surfaces before it becomes a 502 in the middle of an answer.
    assert body["status"] == "degraded"
    assert "is not pulled" in body["issues"][0]


def test_health_degrades_when_ollama_is_unreachable(monkeypatch, provider):
    def refused(*args, **kwargs):
        raise requests.exceptions.ConnectionError("connection refused")

    wire_health(monkeypatch, refused, provider)
    body = client.get("/health").json()

    assert body["status"] == "degraded"
    assert "unreachable" in body["issues"][0]


def test_health_degrades_instead_of_crashing(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("Missing key inputs argument!")

    monkeypatch.setattr(app_module, "get_llm_provider", boom)
    monkeypatch.setattr(app_module, "open_collection", boom)
    body = client.get("/health").json()

    # Before lazy provider init this was unreachable: the process died at import.
    assert body["status"] == "degraded"
    assert len(body["issues"]) == 2
    assert body["chunks_indexed"] == 0


# --- Day 12: the background sync --------------------------------------------


def test_a_changed_sync_clears_the_retrieval_index_cache(monkeypatch):
    """retrieval's cache is keyed on (name, count). One alert resolving as another
    fires leaves the count identical and the content completely different, so the app
    would serve the resolved alert until someone restarted uvicorn."""
    from ingest import Plan
    from retrieval import _index_cache

    _index_cache[("stub", 1)] = object()
    monkeypatch.setattr(app_module, "sync_alerts", lambda: Plan(to_add=["x"]))

    app_module.alert_sync_tick()

    assert _index_cache == {}


def test_an_unchanged_sync_leaves_the_cache_alone(monkeypatch):
    from ingest import Plan
    from retrieval import _index_cache

    sentinel = object()
    _index_cache[("stub", 1)] = sentinel
    monkeypatch.setattr(app_module, "sync_alerts", lambda: Plan(unchanged=["x"]))

    app_module.alert_sync_tick()

    assert _index_cache[("stub", 1)] is sentinel


def test_a_deletion_only_sync_still_clears_the_cache(monkeypatch):
    """An expiring alert changes content without adding anything."""
    from ingest import Plan
    from retrieval import _index_cache

    _index_cache[("stub", 1)] = object()
    monkeypatch.setattr(app_module, "sync_alerts", lambda: Plan(to_delete=["x"]))

    app_module.alert_sync_tick()

    assert _index_cache == {}


def test_an_unreachable_alertmanager_does_not_propagate(monkeypatch):
    """A tick that raises must not kill the loop or reach a request."""
    from connectors.alertmanager import AlertmanagerError

    def boom():
        raise AlertmanagerError("appsrv is down")

    monkeypatch.setattr(app_module, "sync_alerts", boom)

    app_module.alert_sync_tick()  # must not raise


def test_an_unexpected_sync_error_does_not_propagate(monkeypatch):
    def boom():
        raise ValueError("something else entirely")

    monkeypatch.setattr(app_module, "sync_alerts", boom)

    app_module.alert_sync_tick()  # must not raise


def test_no_token_configured_leaves_the_endpoint_open(monkeypatch, wire):
    """The default deployment has no token, and must keep working."""
    monkeypatch.setattr(app_module, "KC_API_TOKEN", "")
    wire([])
    assert client.post("/ask-runbook", json=OOM_QUESTION).status_code == 200


def test_missing_header_is_rejected(monkeypatch):
    monkeypatch.setattr(app_module, "KC_API_TOKEN", "s3cret")
    response = client.post("/ask-runbook", json=OOM_QUESTION)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_malformed_header_is_rejected(monkeypatch):
    """A bare token with no scheme, so the scheme check is what has to catch it."""
    monkeypatch.setattr(app_module, "KC_API_TOKEN", "s3cret")
    response = client.post(
        "/ask-runbook", json=OOM_QUESTION, headers={"Authorization": "s3cret"}
    )
    assert response.status_code == 401


def test_wrong_token_is_rejected(monkeypatch):
    monkeypatch.setattr(app_module, "KC_API_TOKEN", "s3cret")
    response = client.post(
        "/ask-runbook", json=OOM_QUESTION, headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401


def test_correct_token_is_accepted(monkeypatch, wire):
    monkeypatch.setattr(app_module, "KC_API_TOKEN", "s3cret")
    wire([])
    response = client.post(
        "/ask-runbook", json=OOM_QUESTION, headers={"Authorization": "Bearer s3cret"}
    )
    assert response.status_code == 200


def test_health_reports_auth_disabled_when_no_token(monkeypatch):
    monkeypatch.setattr(app_module, "KC_API_TOKEN", "")
    assert client.get("/health").json()["auth"] == "disabled"


def test_health_reports_auth_required_when_token_set(monkeypatch):
    monkeypatch.setattr(app_module, "KC_API_TOKEN", "s3cret")
    assert client.get("/health").json()["auth"] == "required"


def test_metrics_needs_no_token(monkeypatch):
    """Prometheus scrapes over loopback and cannot carry a bearer token."""
    monkeypatch.setattr(app_module, "KC_API_TOKEN", "s3cret")
    assert client.get("/metrics").status_code == 200


def test_slack_route_does_not_require_a_bearer_token(monkeypatch):
    """Slack authenticates with an HMAC signature and cannot send a bearer token.
    Requiring one here would take the bot offline."""
    monkeypatch.setattr(app_module, "KC_API_TOKEN", "s3cret")
    monkeypatch.setattr(app_module, "slack_active", lambda: True)
    response = client.post(
        "/slack/events",
        content=b"{}",
        headers={"X-Slack-Request-Timestamp": "1", "X-Slack-Signature": "v0=nope"},
    )
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers  # rejected by HMAC, not by token
