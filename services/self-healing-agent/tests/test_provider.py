import pytest
from google.genai import errors as genai_errors
from google.genai import types

import provider as provider_module
from errors import AgentProviderError
from provider import GeminiProvider

TOOLS = [
    {
        "name": "get_pod_logs",
        "description": "Read a pod's recent logs.",
        "schema": {
            "type": "object",
            "properties": {"namespace": {"type": "string"}, "pod": {"type": "string"}},
            "required": ["namespace", "pod"],
        },
    }
]


class RecordingModels:
    """Stands in for client.models, and keeps the config it was handed.

    `calls` and `fail_first` exist for the retry: a stub that always raises can prove
    exhaustion but never recovery, and recovery is the behaviour day 20 added. With
    fail_first=2 the third call succeeds, which is the shape of a real 503.
    """

    def __init__(self, response=None, error=None, fail_first=None):
        self.response = response
        self.error = error
        self.fail_first = fail_first
        self.calls = 0
        self.slept: list[float] = []
        self.last_config = None
        self.last_contents = None

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        self.last_config = config
        self.last_contents = contents
        if self.error and (self.fail_first is None or self.calls <= self.fail_first):
            raise self.error
        return self.response


def a_response(*parts):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=list(parts)))
        ]
    )


def a_call(name="get_pod_logs", **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


@pytest.fixture
def gemini(monkeypatch):
    def _make(response=None, error=None, fail_first=None):
        provider = GeminiProvider()
        models = RecordingModels(response=response, error=error, fail_first=fail_first)
        monkeypatch.setattr(provider, "client", type("Shim", (), {"models": models})())
        # Recorded, not endured. Every retry test would otherwise pay the real backoff,
        # and the delays themselves are worth an assertion.
        monkeypatch.setattr(provider_module.time, "sleep", models.slept.append)
        return provider, models

    return _make


def test_automatic_function_calling_is_always_disabled(gemini):
    # The single most important assertion in this service. With AFC enabled -- the SDK's
    # default -- google-genai executes tool functions itself, which would call restart_pod
    # with no human in the path and make day 18's approval gate decorative.
    provider, models = gemini(a_response(types.Part(text="hello")))

    provider.chat("sys", [provider.user("why is it broken")], TOOLS)

    assert models.last_config["automatic_function_calling"].disable is True


def test_tools_are_declared_as_schemas_never_as_callables(gemini):
    # The second defence: even if the flag above regressed, there is no callable for the
    # SDK to invoke.
    provider, models = gemini(a_response(types.Part(text="hello")))

    provider.chat("sys", [], TOOLS)

    declaration = models.last_config["tools"][0].function_declarations[0]
    assert declaration.name == "get_pod_logs"
    assert declaration.parameters_json_schema == TOOLS[0]["schema"]
    # parameters and parameters_json_schema are mutually exclusive in this API; setting
    # both is a 400 from the server, not a local error.
    assert declaration.parameters is None


def test_function_calls_become_tool_calls(gemini):
    provider, _ = gemini(a_response(a_call(namespace="sandbox", pod="api-7f9")))

    result = provider.chat("sys", [], TOOLS)

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "get_pod_logs"
    assert result.tool_calls[0].args == {"namespace": "sandbox", "pod": "api-7f9"}


def test_raw_is_the_models_own_content_object(gemini):
    # Not a rebuilt Content. Gemini rejects a reconstructed turn, so the loop has to echo
    # this object back byte for byte.
    response = a_response(a_call(namespace="sandbox", pod="api-7f9"))
    provider, _ = gemini(response)

    result = provider.chat("sys", [], TOOLS)

    assert result.raw is response.candidates[0].content


def test_thought_parts_are_not_the_answer(gemini):
    provider, _ = gemini(
        a_response(
            types.Part(text="let me check the logs", thought=True),
            types.Part(text="the pod is OOMKilled"),
        )
    )

    result = provider.chat("sys", [], TOOLS)

    assert result.text == "the pod is OOMKilled"


def test_allowed_constrains_the_model_in_validated_mode(gemini):
    provider, models = gemini(a_response(types.Part(text="hello")))

    provider.chat("sys", [], TOOLS, allowed=["get_pod_logs"])

    config = models.last_config["tool_config"].function_calling_config
    # VALIDATED, not ANY: ANY would force a call every turn and leave the model no way to
    # say it is stuck.
    assert config.mode == types.FunctionCallingConfigMode.VALIDATED
    assert config.allowed_function_names == ["get_pod_logs"]


def test_no_allowlist_means_no_tool_config(gemini):
    provider, models = gemini(a_response(types.Part(text="hello")))

    provider.chat("sys", [], TOOLS)

    assert "tool_config" not in models.last_config


def test_empty_candidates_is_an_upstream_error(gemini):
    # A safety filter returns a response with no candidates at all. Reading
    # candidates[0] would be an IndexError surfacing as a 500.
    provider, _ = gemini(types.GenerateContentResponse(candidates=[]))

    with pytest.raises(AgentProviderError) as caught:
        provider.chat("sys", [], TOOLS)

    assert caught.value.status == 502
    assert caught.value.provider == "gemini"


def test_api_error_becomes_an_agent_provider_error(gemini):
    provider, _ = gemini(
        error=genai_errors.APIError(429, {"message": "quota exceeded"})
    )

    with pytest.raises(AgentProviderError) as caught:
        provider.chat("sys", [], TOOLS)

    assert caught.value.provider == "gemini"


def test_a_transient_error_is_retried_until_it_succeeds(gemini):
    # The whole point of day 20's retry. Before it, this 503 discarded a diagnosis that
    # was six model calls deep -- survivable while a human drives, silent data loss once
    # Alertmanager does.
    provider, models = gemini(
        a_response(types.Part(text="the pod is OOMKilled")),
        error=genai_errors.APIError(503, {"message": "model overloaded"}),
        fail_first=2,
    )

    result = provider.chat("sys", [], TOOLS)

    assert result.text == "the pod is OOMKilled"
    assert models.calls == 3


def test_a_permanent_error_is_not_retried(gemini):
    # A 400 is a request this code got wrong. Retrying it sends the same broken request
    # three times and turns one fast failure into three slow ones.
    provider, models = gemini(error=genai_errors.APIError(400, {"message": "bad tool"}))

    with pytest.raises(AgentProviderError):
        provider.chat("sys", [], TOOLS)

    assert models.calls == 1
    assert models.slept == []


def test_retries_are_bounded_and_still_raise(gemini):
    # An outage that outlasts the budget must still surface. A retry that never gives up
    # is an alert nobody gets.
    provider, models = gemini(error=genai_errors.APIError(503, {"message": "down"}))

    with pytest.raises(AgentProviderError) as caught:
        provider.chat("sys", [], TOOLS)

    assert models.calls == provider_module.MAX_RETRIES
    assert caught.value.status == 502
    assert caught.value.provider == "gemini"


def test_each_retry_is_counted_under_the_status_that_caused_it(gemini):
    # A retry counter that never moves is a retry nobody has evidence works, and one
    # that moves constantly is a provider to reconsider. Neither is visible from logs.
    from conftest import metric

    before = metric("sha_model_retries_total", status="503")
    provider, _ = gemini(
        a_response(types.Part(text="ok")),
        error=genai_errors.APIError(503, {"message": "overloaded"}),
        fail_first=2,
    )

    provider.chat("sys", [], TOOLS)

    assert metric("sha_model_retries_total", status="503") == before + 2


def test_backoff_grows_between_attempts(gemini):
    # Constant backoff against a rate limit is three requests into the same closed door.
    provider, models = gemini(error=genai_errors.APIError(429, {"message": "quota"}))

    with pytest.raises(AgentProviderError):
        provider.chat("sys", [], TOOLS)

    # One sleep fewer than attempts: nothing sleeps after the last failure.
    assert len(models.slept) == provider_module.MAX_RETRIES - 1
    assert models.slept == sorted(models.slept)
    assert models.slept[0] < models.slept[-1]


def test_tool_result_is_a_function_response(gemini):
    provider, _ = gemini(a_response(types.Part(text="hello")))
    from provider import ToolCall

    content = provider.tool_result(
        ToolCall(name="get_pod_logs", args={}), {"output": "OOMKilled"}
    )

    assert content.role == "user"
    assert content.parts[0].function_response.name == "get_pod_logs"
    assert content.parts[0].function_response.response == {"output": "OOMKilled"}
