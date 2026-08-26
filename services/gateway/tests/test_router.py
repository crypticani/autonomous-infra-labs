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
    """get_router_provider is lru_cached, so patch the name router.py imported."""

    def install(answer: str):
        fake = FakeProvider(answer)
        monkeypatch.setattr(router, "get_router_provider", lambda: fake)
        return fake

    return install


# --- the schema is the guard ---


def test_the_schema_offers_the_four_services_and_nothing_else():
    """One flat `enum`, so an invented name is unrepresentable rather than rejected."""
    field = Route.model_json_schema()["properties"]["service"]
    assert field["enum"] == list(backends.NAMES) + [NONE]
    assert "anyOf" not in field


def test_reason_is_generated_before_the_service_it_explains():
    """Generation is left to right, so this order makes the explanation a premise."""
    assert list(Route.model_json_schema()["properties"]) == [
        "reason",
        "service",
        "confidence",
    ]


def test_a_service_outside_the_table_does_not_validate():
    with pytest.raises(ValidationError):
        Route(reason="x", service="grafana", confidence="high")


def test_an_over_long_reason_is_truncated_not_rejected():
    """A grammar enforces neither maxLength nor `ge/le`, so a cap here would 502 whenever
    the model ran long -- measured once at 199 of 200 characters. `reason` is prose for a
    human, so clipping costs nothing and a failed route costs the request."""
    route = Route(reason="x" * 400, service="log-analyzer", confidence="high")
    assert len(route.reason) == router.REASON_CHARS

    # And the schema no longer advertises a constraint nothing enforces.
    assert "maxLength" not in Route.model_json_schema()["properties"]["reason"]


def test_confidence_is_an_enum_the_grammar_can_enforce():
    """A grammar enforces membership but not numeric bounds, so `ge/le` was a post-hoc
    rejection both models tripped. As an enum it is unrepresentable."""
    field = Route.model_json_schema()["properties"]["confidence"]
    assert field["enum"] == list(router.LEVELS)
    assert field["type"] == "string"

    with pytest.raises(ValidationError):
        Route(reason="x", service="log-analyzer", confidence=1.4)
    with pytest.raises(ValidationError):
        Route(reason="x", service="log-analyzer", confidence="very")


# --- the prompt ---


def test_the_system_prompt_lists_every_service():
    for backend in backends.BACKENDS:
        assert backend.name in router.SYSTEM_PROMPT
    assert NONE in router.SYSTEM_PROMPT


def test_the_prompt_defines_the_levels_without_naming_the_bar():
    """The levels have to appear or the model is guessing at an enum. The bar stays out:
    name a threshold and the model reports one notch above it."""
    for level in router.LEVELS:
        assert level in router.SYSTEM_PROMPT

    leaks = ("or above", "or higher", "at least", f"under {router.ROUTE_ON}")
    for phrase in leaks:
        assert phrase not in router.SYSTEM_PROMPT.lower(), phrase


def test_a_question_with_no_attachment_carries_no_attachment_block():
    prompt = build_user_prompt("why is checkout slow")
    assert "<question>" in prompt
    assert "attachment" not in prompt


def test_an_attachment_reaches_the_router_truncated(monkeypatch):
    """Enough to be evidence, not enough to pay LLM prices for."""
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
                "confidence": "high",
            }
        )
    )

    route = classify("what does this OOMKilled line mean", "some log text")

    assert route.service == "log-analyzer"
    assert route.confidence == "high"
    # The schema goes to the provider as a class, so each backend can ask it for the shape
    # it wants -- Ollama the JSON schema, Gemini the class itself.
    assert fake.schema is Route
    assert "some log text" in fake.user


def test_non_json_is_a_502_and_never_a_default_route(use):
    """A default route would be the behaviour `none` exists to prevent, chosen by a bug."""
    use("I think this is probably the log analyzer?")

    with pytest.raises(GatewayProviderError) as caught:
        classify("what broke")
    assert caught.value.status == 502


def test_a_valid_shape_with_an_invalid_service_is_a_502(use):
    """Belt to the schema's braces: a backend that ignored `format` gets caught here."""
    use(json.dumps({"reason": "x", "service": "prometheus", "confidence": "high"}))

    with pytest.raises(GatewayProviderError) as caught:
        classify("what broke")
    assert caught.value.status == 502


# --- decision, which is pure ---


def test_the_model_declining_is_a_decline():
    route = Route(reason="could be either", service=NONE, confidence="high")
    name, note = decision(route)

    assert name is None
    assert "could not place" in note


def test_a_confident_route_passes_through_with_nothing_added():
    route = Route(
        reason="asks what a log means", service="log-analyzer", confidence="high"
    )
    assert decision(route) == ("log-analyzer", None)


def test_the_floor_overrides_a_service_the_model_named():
    """The failure mode the field exists to catch: picking something over nothing."""
    route = Route(reason="a guess", service="security-triage", confidence="low")
    name, note = decision(route)

    assert name is None
    assert "low" in note
    # Named, not hidden: the caller can decide the router was right and retry explicitly.
    assert "security-triage" in note


def test_the_floor_is_inclusive_at_its_own_level(monkeypatch):
    monkeypatch.setattr(router, "ROUTE_ON", "medium")
    route = Route(reason="borderline", service="log-analyzer", confidence="medium")
    assert decision(route)[0] == "log-analyzer"


def test_raising_the_bar_to_high_rejects_medium(monkeypatch):
    """Ranked by position, not as strings: `"medium" < "high"` is false alphabetically."""
    assert router.LEVELS == ("low", "medium", "high")
    monkeypatch.setattr(router, "ROUTE_ON", "high")

    medium = Route(reason="probably", service="log-analyzer", confidence="medium")
    high = Route(reason="plainly", service="log-analyzer", confidence="high")

    assert decision(medium)[0] is None
    assert decision(high)[0] == "log-analyzer"


def test_an_unknown_route_on_value_refuses_to_load():
    """A typo here is a gateway with no floor at all, which is what the constant exists to
    prevent -- so it fails rather than quietly accepting everything."""
    assert router._validated_level("  HIGH ") == "high"

    with pytest.raises(ValueError, match="GW_ROUTE_ON"):
        router._validated_level("quite-sure")
    with pytest.raises(ValueError, match="GW_ROUTE_ON"):
        router._validated_level("0.6")


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
