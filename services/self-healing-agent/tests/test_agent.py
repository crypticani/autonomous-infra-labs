import pytest

import agent
import guardrails
import tools.external as external
from conftest import metric
from errors import AgentProviderError, GuardrailViolation
from provider import AgentTurn, ToolCall

ALERT = {"alertname": "CrashLoopBackOff", "service": "checkout-api"}


def turn(text="", calls=()):
    return AgentTurn(text=text, tool_calls=tuple(calls), raw={"echo": text})


def call(name, **args):
    return ToolCall(name=name, args=args)


def test_refuses_a_tool_outside_the_allowlist_and_keeps_going(fake_provider):
    provider = fake_provider(
        turn(calls=[call("restart_pod", namespace="sandbox", pod="checkout-7f9")]),
        turn(
            calls=[
                call(
                    "submit_diagnosis",
                    summary="checkout-api is crashlooping",
                    evidence=["restart_pod was refused"],
                    confidence=0.4,
                )
            ]
        ),
    )

    diagnosis = agent.diagnose(ALERT, provider)

    assert diagnosis == agent.Diagnosis(
        summary="checkout-api is crashlooping",
        evidence=("restart_pod was refused",),
        proposed_action=None,
        confidence=0.4,
        incomplete=False,
    )
    # The refusal has to reach the transcript, not just stop the dispatch -- otherwise
    # the model, and the audit log, can't tell "refused" from "nothing happened".
    refusal = provider.seen_contents[1][-1]
    assert refusal["name"] == "restart_pod"
    assert "not an available tool" in refusal["result"]["error"]


def test_max_iterations_exhausted_yields_no_fabricated_diagnosis(fake_provider):
    provider = fake_provider(
        *[turn(text="still investigating") for _ in range(agent.MAX_ITERATIONS)]
    )

    diagnosis = agent.diagnose(ALERT, provider)

    assert diagnosis.incomplete is True
    assert diagnosis.confidence is None
    assert diagnosis.summary is None
    assert diagnosis.evidence == ()
    assert provider.calls == agent.MAX_ITERATIONS


class FakeCopilotResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"hits": [{"text": "raise the memory limit"}]}


def test_dispatches_an_allowed_tool_and_feeds_back_its_result(
    fake_provider, monkeypatch
):
    monkeypatch.setattr(agent.k8s_client, "get_apis", lambda: (None, None))
    monkeypatch.setattr(
        external.requests, "post", lambda *a, **k: FakeCopilotResponse()
    )
    provider = fake_provider(
        turn(calls=[call("search_runbooks", question="checkout OOMKilled")]),
        turn(
            calls=[
                call(
                    "submit_diagnosis",
                    summary="checkout-api needs a higher memory limit",
                    evidence=["runbook: raise the memory limit"],
                    confidence=0.8,
                )
            ]
        ),
    )

    diagnosis = agent.diagnose(ALERT, provider)

    assert diagnosis.confidence == 0.8
    fed_back = provider.seen_contents[1][-1]
    assert fed_back["result"]["output"]["hits"][0]["text"] == "raise the memory limit"


def submits(summary="checkout-api is crashlooping", confidence=0.4):
    return turn(
        calls=[
            call(
                "submit_diagnosis",
                summary=summary,
                evidence=["logs"],
                confidence=confidence,
            )
        ]
    )


def test_a_finished_diagnosis_counts_as_complete(fake_provider):
    before = metric("sha_diagnoses_total", outcome="complete")

    agent.diagnose(ALERT, fake_provider(submits()))

    assert metric("sha_diagnoses_total", outcome="complete") == before + 1


def test_an_exhausted_loop_counts_as_incomplete_not_complete(fake_provider):
    # The distinction the counter exists for. An incomplete diagnosis is still a returned
    # Diagnosis, so a single sha_diagnoses_total would call this a success and hide the
    # only failure mode this loop has that raises nothing.
    before = metric("sha_diagnoses_total", outcome="incomplete")
    provider = fake_provider(
        *[turn(text="still investigating") for _ in range(agent.MAX_ITERATIONS)]
    )

    agent.diagnose(ALERT, provider)

    assert metric("sha_diagnoses_total", outcome="incomplete") == before + 1


def test_a_guardrail_refusal_counts_as_blocked(fake_provider, monkeypatch):
    before = metric("sha_diagnoses_total", outcome="blocked")

    def refuse(now=None):
        raise GuardrailViolation("budget spent", guard="llm_calls")

    monkeypatch.setattr(guardrails, "check_llm_call", refuse)

    with pytest.raises(GuardrailViolation):
        agent.diagnose(ALERT, fake_provider(submits()))

    assert metric("sha_diagnoses_total", outcome="blocked") == before + 1


def test_an_upstream_failure_counts_as_failed_not_blocked(fake_provider):
    # Blocked is the agent deciding not to act; failed is something else breaking. One
    # is a working guardrail and the other is a page, so collapsing them would make the
    # metric unactionable.
    before = metric("sha_diagnoses_total", outcome="failed")

    class Broken(type(fake_provider(submits()))):
        def chat(self, *a, **k):
            raise AgentProviderError("model is down", 502, provider="fake")

    with pytest.raises(AgentProviderError):
        agent.diagnose(ALERT, Broken([]))

    assert metric("sha_diagnoses_total", outcome="failed") == before + 1


def test_a_diagnosis_is_timed_even_when_it_raises(fake_provider):
    # A histogram that only observes the happy path makes an outage look like a quiet
    # afternoon: the failures cost real seconds and simply never appear.
    before = metric("sha_diagnosis_duration_seconds_count")
    agent.diagnose(ALERT, fake_provider(submits()))

    class Broken(type(fake_provider(submits()))):
        def chat(self, *a, **k):
            raise AgentProviderError("model is down", 502, provider="fake")

    with pytest.raises(AgentProviderError):
        agent.diagnose(ALERT, Broken([]))

    assert metric("sha_diagnosis_duration_seconds_count") == before + 2
