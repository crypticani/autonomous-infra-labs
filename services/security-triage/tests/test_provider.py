from types import SimpleNamespace

import pytest
import requests
from google.genai import errors as genai_errors
from pydantic import BaseModel

from errors import TriageProviderError
from provider import GeminiProvider, MAX_TOKENS, OllamaProvider


class ASchema(BaseModel):
    results: list[str]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


@pytest.mark.parametrize(
    "error, status",
    [
        (requests.exceptions.Timeout("slow"), 504),
        (requests.exceptions.HTTPError("404 model not found"), 502),
        (requests.exceptions.ConnectionError("refused"), 503),
    ],
)
def test_ollama_transport_failures_carry_the_right_status(monkeypatch, error, status):
    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(TriageProviderError) as caught:
        OllamaProvider().generate("system", "user", schema=ASchema)

    assert caught.value.status == status
    assert caught.value.provider == "ollama"


def test_ollama_empty_body_is_a_502(monkeypatch):
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: FakeResponse({"response": " "})
    )
    with pytest.raises(TriageProviderError) as caught:
        OllamaProvider().generate("system", "user", schema=ASchema)
    assert caught.value.status == 502


def test_ollama_sends_the_schema_as_format(monkeypatch):
    sent = {}

    def capture(url, json, timeout):
        sent.update(json)
        return FakeResponse({"response": "ok"})

    monkeypatch.setattr(requests, "post", capture)
    OllamaProvider().generate("sys", "usr", schema=ASchema)

    assert sent["format"] == ASchema.model_json_schema()
    # Ollama ignores a top-level temperature key; it reads "options". num_predict and
    # repeat_penalty exist because greedy decoding with no repetition penalty found a
    # real runaway loop live on 2026-08-19 -- generating 3,613+ tokens for a batch that
    # should have produced ~750, never emitting a stop token.
    assert sent["options"] == {
        "temperature": 0.0,
        "num_predict": MAX_TOKENS,
        "repeat_penalty": 1.3,
    }


def test_ollama_returns_the_raw_text(monkeypatch):
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: FakeResponse({"response": '  {"x": 1}  '})
    )
    assert OllamaProvider().generate("sys", "usr", schema=ASchema) == '{"x": 1}'


class RecordingModels:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.last_config = None

    def generate_content(self, *, model, contents, config):
        self.last_config = config
        if self.error:
            raise self.error
        return self.response


class FakeGeminiResponse:
    def __init__(self, text, usage=None):
        self.text = text
        # None is the real shape when a request never reached billing -- a safety block --
        # not a convenience for the tests.
        self.usage_metadata = usage


def usage(prompt=0, candidates=0, thoughts=0):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=candidates,
        thoughts_token_count=thoughts,
    )


@pytest.fixture
def gemini(monkeypatch):
    def _make(response=None, error=None):
        provider = GeminiProvider()
        models = RecordingModels(response=response, error=error)
        monkeypatch.setattr(provider, "client", type("Shim", (), {"models": models})())
        return provider, models

    return _make


def test_gemini_returns_the_raw_text(gemini):
    provider, _ = gemini(response=FakeGeminiResponse('{"results": []}'))
    assert provider.generate("sys", "usr", schema=ASchema) == '{"results": []}'


def test_gemini_requests_json_constrained_to_the_schema(gemini):
    provider, models = gemini(response=FakeGeminiResponse("{}"))
    provider.generate("sys", "usr", schema=ASchema)

    assert models.last_config["response_mime_type"] == "application/json"
    assert models.last_config["response_schema"] is ASchema


def test_gemini_empty_text_is_a_502(gemini):
    provider, _ = gemini(response=FakeGeminiResponse(" "))
    with pytest.raises(TriageProviderError) as caught:
        provider.generate("sys", "usr", schema=ASchema)
    assert caught.value.status == 502


def test_gemini_api_error_becomes_a_triage_provider_error(gemini):
    provider, _ = gemini(error=genai_errors.APIError(502, {"message": "overloaded"}))
    with pytest.raises(TriageProviderError) as caught:
        provider.generate("sys", "usr", schema=ASchema)
    assert caught.value.provider == "gemini"


def test_ollama_accumulates_tokens_across_calls(monkeypatch):
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: FakeResponse(
            {"response": "ok", "prompt_eval_count": 400, "eval_count": 90}
        ),
    )
    provider = OllamaProvider()
    provider.generate("sys", "usr", schema=ASchema)
    provider.generate("sys", "usr", schema=ASchema)

    # Cumulative, not last-call: bench.py reads a delta around a whole config, and two
    # calls that each overwrote the counters would report one call's worth of tokens.
    assert (provider.prompt_tokens, provider.output_tokens) == (800, 180)


def test_ollama_tokens_survive_a_response_without_counts(monkeypatch):
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: FakeResponse({"response": "ok"})
    )
    provider = OllamaProvider()
    provider.generate("sys", "usr", schema=ASchema)
    assert (provider.prompt_tokens, provider.output_tokens) == (0, 0)


def test_gemini_counts_thinking_tokens_as_output(gemini):
    provider, _ = gemini(
        response=FakeGeminiResponse(
            '{"results": []}', usage=usage(prompt=7, candidates=1, thoughts=119)
        )
    )
    provider.generate("sys", "usr", schema=ASchema)

    # The measurement this exists for, taken live on 2026-08-22: 1 candidate token and
    # 119 thinking tokens for a one-word answer, all billed at the output rate. Reading
    # candidates_token_count alone reports 1 and makes Gemini look ~120x cheaper than it
    # is, which is the whole cost table wrong in the cheap direction.
    assert provider.prompt_tokens == 7
    assert provider.output_tokens == 120


def test_gemini_without_usage_metadata_counts_nothing(gemini):
    provider, _ = gemini(response=FakeGeminiResponse('{"results": []}'))
    provider.generate("sys", "usr", schema=ASchema)
    assert (provider.prompt_tokens, provider.output_tokens) == (0, 0)


def test_counters_are_per_instance_not_shared():
    # The class-level defaults are ints so `+=` rebinds on the instance. If they were
    # ever changed to a dict, every provider in the process would share one and this
    # fails.
    first, second = OllamaProvider(), OllamaProvider()
    first.prompt_tokens += 10
    assert second.prompt_tokens == 0
