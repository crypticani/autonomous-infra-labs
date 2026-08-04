import pytest
import requests
from fastapi.testclient import TestClient

import app as app_module
from app import NOT_COVERED, app
from llm import UpstreamError
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


class SpyLLM:
    """Counts calls, because 'we skipped the model' can only be proven this way."""

    name = "spy"
    model_name = "spy-model"

    def __init__(self, answer="", error=None):
        self.answer = answer
        self.error = error
        self.calls = 0
        self.last_prompts = ()

    def generate(self, system_prompt, user_prompt, temperature=0.1) -> str:
        self.calls += 1
        self.last_prompts = (system_prompt, user_prompt)
        if self.error:
            raise self.error
        return self.answer


@pytest.fixture
def wire(monkeypatch):
    """Replace both upstreams: no Chroma client, no model, no network."""

    def _wire(hits, answer="", error=None):
        spy = SpyLLM(answer=answer, error=error)
        monkeypatch.setattr(app_module, "get_llm_provider", lambda: spy)
        monkeypatch.setattr(app_module, "open_collection", lambda: (None, None))
        monkeypatch.setattr(app_module, "retrieve", lambda *args, **kwargs: hits)
        return spy

    return _wire


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
