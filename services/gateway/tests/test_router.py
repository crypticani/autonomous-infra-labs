import json

import pytest
from pydantic import ValidationError

import backends
import provider as provider_module
import router
from errors import GatewayProviderError
from router import NONE, Route, build_user_prompt, classify, decision


class FakeProvider:
    """Returns whatever text the test wants, and records what it was asked."""

    name = "fake"
    model_name = "fake-model"

    def __init__(self, answer: str):
        self.answer = answer
        self.system = self.user = ""
        self.schema = None

    def generate(self, system, user, schema):
        self.system, self.user, self.schema = system, user, schema
        return self.answer


@pytest.fixture
def use(monkeypatch):
    """Install a fake provider. get_router_provider is lru_cached, so patching the name
    router.py imported is the only version of this that actually takes effect."""

    def install(answer: str):
        fake = FakeProvider(answer)
        monkeypatch.setattr(router, "get_router_provider", lambda: fake)
        return fake

    return install


# --- the schema is the guard ---


def test_the_schema_offers_the_four_services_and_nothing_else():
    """An invented service name is not validated away after the fact -- it is
    unrepresentable. Both providers get this as one flat `enum`, which is why `none` is a
    member of it rather than the field being nullable."""
    field = Route.model_json_schema()["properties"]["service"]
    assert field["enum"] == list(backends.NAMES) + [NONE]
    assert "anyOf" not in field


def test_reason_is_generated_before_the_service_it_explains():
    """Field order is behaviour, not formatting. Generation is left to right, so this order
    makes the explanation a premise; reverse it and the model picks first and writes
    whatever justifies the pick, which reads identically and is worth nothing."""
    assert list(Route.model_json_schema()["properties"]) == [
        "reason",
        "service",
        "confidence",
    ]


def test_a_service_outside_the_table_does_not_validate():
    with pytest.raises(ValidationError):
        Route(reason="x", service="grafana", confidence=0.9)


def test_reason_is_capped_rather_than_asked_nicely():
    """Day 26 asked the prompt for `one short sentence` and shipped 270-character
    paragraphs for a month."""
    with pytest.raises(ValidationError):
        Route(reason="x" * 201, service="log-analyzer", confidence=0.9)


def test_confidence_outside_zero_to_one_does_not_validate():
    with pytest.raises(ValidationError):
        Route(reason="x", service="log-analyzer", confidence=1.4)


# --- the prompt ---


def test_the_system_prompt_lists_every_service():
    for backend in backends.BACKENDS:
        assert backend.name in router.SYSTEM_PROMPT
    assert NONE in router.SYSTEM_PROMPT


def test_the_system_prompt_does_not_name_the_confidence_floor():
    """Telling a model the threshold is how you get a model that reports one point above
    it. The prompt says a low score means the gateway declines, and leaves the number out.
    """
    assert str(router.MIN_CONFIDENCE) not in router.SYSTEM_PROMPT


def test_a_question_with_no_attachment_carries_no_attachment_block():
    prompt = build_user_prompt("why is checkout slow")
    assert "<question>" in prompt
    assert "attachment" not in prompt


def test_an_attachment_reaches_the_router_truncated(monkeypatch):
    """An attachment is real evidence -- a JSON alert is almost certainly the agent's -- and
    the first couple of hundred characters carry all of that signal. Sending a 16 MiB scan
    envelope to a classifier would be paying LLM prices to reread what a dict lookup
    knows."""
    monkeypatch.setattr(router, "ATTACHMENT_PREVIEW", 20)
    prompt = build_user_prompt("what is this", "A" * 500)

    assert "A" * 20 in prompt
    assert "A" * 21 not in prompt
    assert "truncated" in prompt


# --- classify ---


def test_a_good_answer_becomes_a_route(use):
    fake = use(
        json.dumps(
            {
                "reason": "asks what a log means",
                "service": "log-analyzer",
                "confidence": 0.88,
            }
        )
    )

    route = classify("what does this OOMKilled line mean", "some log text")

    assert route.service == "log-analyzer"
    assert route.confidence == 0.88
    # The schema goes to the provider as a class, so each backend can ask it for the shape
    # it wants -- Ollama the JSON schema, Gemini the class itself.
    assert fake.schema is Route
    assert "some log text" in fake.user


def test_non_json_is_a_502_and_never_a_default_route(use):
    """There is no safe service to fall back to. Picking one would be exactly the behaviour
    `none` exists to prevent, decided by a bug instead of by the model."""
    use("I think this is probably the log analyzer?")

    with pytest.raises(GatewayProviderError) as caught:
        classify("what broke")
    assert caught.value.status == 502


def test_a_valid_shape_with_an_invalid_service_is_a_502(use):
    """Belt to the schema's braces: a backend that ignored `format` gets caught here."""
    use(json.dumps({"reason": "x", "service": "prometheus", "confidence": 0.9}))

    with pytest.raises(GatewayProviderError) as caught:
        classify("what broke")
    assert caught.value.status == 502


# --- decision, which is pure ---


def test_the_model_declining_is_a_decline():
    route = Route(reason="could be either", service=NONE, confidence=0.9)
    name, note = decision(route)

    assert name is None
    assert "could not place" in note


def test_a_confident_route_passes_through_with_nothing_added():
    route = Route(
        reason="asks what a log means", service="log-analyzer", confidence=0.9
    )
    assert decision(route) == ("log-analyzer", None)


def test_the_floor_overrides_a_service_the_model_named():
    """The failure mode a confidence field exists to catch: a model that picks something
    rather than nothing."""
    route = Route(reason="a guess", service="security-triage", confidence=0.4)
    name, note = decision(route)

    assert name is None
    assert "0.40" in note
    assert "security-triage" in note


def test_the_floor_is_inclusive_at_its_own_value(monkeypatch):
    monkeypatch.setattr(router, "MIN_CONFIDENCE", 0.6)
    route = Route(reason="borderline", service="log-analyzer", confidence=0.6)
    assert decision(route)[0] == "log-analyzer"


def test_the_provider_seam_still_reports_tokens():
    """eval_router.py reads these as a delta to price a routing decision, so they have to
    be cumulative on the instance and not just fed to Prometheus."""
    provider = provider_module.BaseRouterProvider.__new__(
        provider_module.OllamaProvider
    )
    provider.prompt_tokens = provider.output_tokens = 0

    provider._count(120, 30)
    provider._count(118, 28)

    assert (provider.prompt_tokens, provider.output_tokens) == (238, 58)
