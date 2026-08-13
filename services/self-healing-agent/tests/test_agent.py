import agent
import tools.external as external
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
