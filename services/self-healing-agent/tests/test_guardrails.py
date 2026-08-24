"""Day 19: the refusals. Every test here asks one question -- would this action have been
allowed, and does the record say which rule stopped it.
"""

import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import approvals
import audit
import guardrails
from agent import Diagnosis
from errors import GuardrailViolation, K8sError
from tools import REGISTRY

RESTART = {"namespace": "sandbox", "pod": "checkout-api-7d9f-x2k"}
SCALE = {"namespace": "sandbox", "deployment": "checkout-api", "replicas": 4}


def apis_with_replicas(count: int):
    """A fake `(core, apps)` pair whose scale subresource reports `count`."""
    scale = SimpleNamespace(spec=SimpleNamespace(replicas=count))
    apps = SimpleNamespace(
        read_namespaced_deployment_scale=lambda name, namespace: scale
    )
    return (None, apps)


def outcomes(*events: str) -> None:
    """Writes a history: `outcomes("failed", "failed")` is what two failed executions left
    behind. Uses audit.record, so a change to the log's shape breaks this.
    """
    for event in events:
        audit.record(event, id="prior", tool="restart_pod")


# --- the config guards -------------------------------------------------------------


def test_a_namespace_outside_the_allowlist_is_refused(audit_log):
    with pytest.raises(GuardrailViolation) as raised:
        guardrails.check("restart_pod", {"namespace": "kube-system", "pod": "etcd-0"})

    assert raised.value.guard == "namespace"


def test_a_missing_namespace_is_refused_as_firmly_as_a_wrong_one(audit_log):
    """Fail-closed: `args.get("namespace")` returning None must not compare equal to
    anything in the allowlist."""
    with pytest.raises(GuardrailViolation) as raised:
        guardrails.check("restart_pod", {"pod": "checkout-api-7d9f-x2k"})

    assert raised.value.guard == "namespace"


def test_the_allowed_namespace_passes_every_config_guard(audit_log):
    guardrails.check("restart_pod", RESTART)
    guardrails.check("scale_deployment", SCALE)


@pytest.mark.parametrize("replicas", [0, -1, None, "two"])
def test_scaling_below_the_floor_is_refused_and_not_clamped(replicas, audit_log):
    """tools/k8s.py clamps a model-chosen count. This does not: the number is already a button,
    and turning 0 into 1 executes something nobody approved.
    """
    with pytest.raises(GuardrailViolation) as raised:
        guardrails.check("scale_deployment", {**SCALE, "replicas": replicas})

    assert raised.value.guard == "replica_floor"


def test_the_replica_floor_does_not_apply_to_other_tools(audit_log):
    """restart_pod has no `replicas` argument, and a guard that demanded one would refuse
    every restart."""
    guardrails.check("restart_pod", RESTART)


# --- the live check, which is why guards run twice ---------------------------------


def test_a_scale_down_against_the_live_count_is_refused_at_execute_time(audit_log):
    """Why guards run twice: the proposal was an increase from 2 when made, a human scaled to 8
    during the incident, and the click twenty minutes later is now a scale *down*.
    """
    with pytest.raises(GuardrailViolation) as raised:
        guardrails.check("scale_deployment", SCALE, apis=apis_with_replicas(8))

    assert raised.value.guard == "live_replicas"
    assert "down from 8 to 4" in str(raised.value)


def test_the_same_proposal_passes_when_the_live_count_has_not_moved(audit_log):
    guardrails.check("scale_deployment", SCALE, apis=apis_with_replicas(2))


def test_without_apis_the_live_count_is_never_read(audit_log):
    """propose() passes no client, and must not need one. If this called the cluster it
    would raise AttributeError on the None in place of `apps`."""
    guardrails.check("scale_deployment", SCALE)


def test_a_cluster_read_that_fails_refuses_rather_than_allows(audit_log):
    """Fail-closed. A current state that cannot be established cannot be checked
    against, so the answer is no -- not "proceed and hope"."""

    def explode(name, namespace):
        raise K8sError("deployments.apps 'checkout-api' is forbidden", status=403)

    apis = (None, SimpleNamespace(read_namespaced_deployment_scale=explode))

    with pytest.raises(GuardrailViolation) as raised:
        guardrails.check("scale_deployment", SCALE, apis=apis)

    assert raised.value.guard == "live_replicas"
    assert "forbidden" in str(raised.value)


# --- the stateful guards, read from the audit log ----------------------------------


def test_the_hourly_budget_stops_the_fourth_action(audit_log, monkeypatch):
    monkeypatch.setattr(guardrails, "MAX_ACTIONS_PER_HOUR", 3)
    outcomes("executed", "executed", "executed")

    with pytest.raises(GuardrailViolation) as raised:
        guardrails.check("restart_pod", RESTART)

    assert raised.value.guard == "rate_limit"


def test_a_failed_execution_still_spends_the_budget(audit_log, monkeypatch):
    """It reached the API server. Counting only successes would let a tool that fails
    every time retry all night."""
    monkeypatch.setattr(guardrails, "MAX_ACTIONS_PER_HOUR", 2)
    monkeypatch.setattr(guardrails, "BREAKER_THRESHOLD", 0)  # isolate the rate limit
    outcomes("failed", "executed")

    with pytest.raises(GuardrailViolation) as raised:
        guardrails.check("restart_pod", RESTART)

    assert raised.value.guard == "rate_limit"


def test_proposals_that_never_ran_do_not_spend_the_budget(audit_log, monkeypatch):
    """Why the count reads `executed`/`failed` and not `approved`: a blocked proposal filling
    the hour's budget would let one bad proposal poison the window.
    """
    monkeypatch.setattr(guardrails, "MAX_ACTIONS_PER_HOUR", 1)
    for event in ("proposed", "approved", "rejected", "blocked", "expired"):
        audit.record(event, id="prior", tool="restart_pod")

    guardrails.check("restart_pod", RESTART)


def test_the_budget_forgets_what_fell_out_of_the_window(audit_log, monkeypatch):
    monkeypatch.setattr(guardrails, "MAX_ACTIONS_PER_HOUR", 1)
    outcomes("executed")
    later = time.time() + guardrails.WINDOW + 1

    guardrails.check("restart_pod", RESTART, now=later)


def test_the_breaker_opens_after_consecutive_failures(audit_log, monkeypatch):
    monkeypatch.setattr(guardrails, "MAX_ACTIONS_PER_HOUR", 99)  # isolate the breaker
    monkeypatch.setattr(guardrails, "BREAKER_THRESHOLD", 3)
    outcomes("failed", "failed", "failed")

    with pytest.raises(GuardrailViolation) as raised:
        guardrails.check("restart_pod", RESTART)

    assert raised.value.guard == "breaker"


def test_the_breaker_is_reachable_at_the_shipped_defaults(audit_log):
    """No monkeypatching: the one breaker test at the real config, and it exists because every
    other one hid a bug behind MAX_ACTIONS_PER_HOUR=99 "to isolate the breaker".
    """
    assert guardrails.BREAKER_THRESHOLD >= guardrails.MAX_ACTIONS_PER_HOUR, (
        "this test is only interesting while the breaker's threshold is the harder one "
        "to reach -- if that changes, the ordering bug it guards cannot happen"
    )
    outcomes(*["failed"] * guardrails.BREAKER_THRESHOLD)

    with pytest.raises(GuardrailViolation) as raised:
        guardrails.check("restart_pod", RESTART)

    assert raised.value.guard == "breaker"


def test_a_success_in_between_keeps_the_breaker_closed(audit_log, monkeypatch):
    """Consecutive, not cumulative. Three failures across a working afternoon are a
    flaky cluster, not a broken agent."""
    monkeypatch.setattr(guardrails, "MAX_ACTIONS_PER_HOUR", 99)
    monkeypatch.setattr(guardrails, "BREAKER_THRESHOLD", 3)
    outcomes("failed", "failed", "executed", "failed")

    guardrails.check("restart_pod", RESTART)


def test_the_breaker_half_opens_once_the_failures_age_out(audit_log, monkeypatch):
    """Why the breaker shares the rate limit's window: without a time bound it blocks the only
    event that could close it and stays open until someone restarts the process.
    """
    monkeypatch.setattr(guardrails, "MAX_ACTIONS_PER_HOUR", 99)
    monkeypatch.setattr(guardrails, "BREAKER_THRESHOLD", 3)
    outcomes("failed", "failed", "failed")
    later = time.time() + guardrails.WINDOW + 1

    guardrails.check("restart_pod", RESTART, now=later)


def test_a_missing_audit_log_is_an_empty_history(tmp_path, monkeypatch):
    """First action after a fresh deploy. Nothing has been executed, so nothing can have
    been exceeded -- and the guards must not raise on the absent file."""
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "nothing-here.jsonl"))

    guardrails.check("restart_pod", RESTART)


def test_an_unreadable_audit_log_refuses_everything(tmp_path, monkeypatch):
    """Deliberate: a history this cannot read is a history it cannot check, and a guard that
    cannot check must not shrug and allow.
    """
    path = tmp_path / "audit.jsonl"
    path.write_text("{this is not json}\n")
    monkeypatch.setattr(audit, "AUDIT_PATH", str(path))

    with pytest.raises(json.JSONDecodeError):
        guardrails.check("restart_pod", RESTART)


# --- the model-call budget --------------------------------------------------------


def test_the_llm_budget_refuses_the_call_past_the_cap(monkeypatch):
    monkeypatch.setattr(guardrails, "MAX_LLM_CALLS", 3)

    for _ in range(3):
        guardrails.check_llm_call()

    with pytest.raises(GuardrailViolation) as raised:
        guardrails.check_llm_call()

    assert raised.value.guard == "llm_calls"


def test_the_llm_budget_refills_as_the_window_rolls(monkeypatch):
    monkeypatch.setattr(guardrails, "MAX_LLM_CALLS", 2)
    now = time.time()
    guardrails.check_llm_call(now)
    guardrails.check_llm_call(now)

    guardrails.check_llm_call(now + guardrails.WINDOW + 1)


def test_the_loop_stops_when_the_budget_is_gone(fake_provider, monkeypatch):
    """It raises rather than returning an incomplete diagnosis: "spent" is a different answer
    from "could not work it out", and a caller that cannot tell will retry the wrong one.
    """
    from agent import diagnose

    monkeypatch.setattr(guardrails, "MAX_LLM_CALLS", 0)

    with pytest.raises(GuardrailViolation):
        diagnose({"labels": {"alertname": "HighLatency"}}, fake_provider())


# --- the two integration points ---------------------------------------------------


def diagnosis(action) -> Diagnosis:
    return Diagnosis(
        summary="checkout-api is unhealthy",
        evidence=("restarted 4m ago",),
        proposed_action=action,
        confidence=0.9,
        incomplete=False,
    )


@pytest.fixture(autouse=True)
def isolated_proposals(monkeypatch):
    approvals._proposals.clear()
    monkeypatch.setattr(approvals, "slack_enabled", lambda: False)
    yield
    approvals._proposals.clear()


def test_a_blocked_action_never_becomes_a_button(audit_log):
    """propose() returns None, so /diagnose answers with proposal_id null and no message
    is ever posted. The refusal is still on disk, with the guard that made it."""
    action = {
        "tool": "restart_pod",
        "args": {"namespace": "kube-system", "pod": "etcd-0"},
    }

    assert approvals.propose(diagnosis(action), {}) is None
    assert approvals._proposals == {}

    (line,) = audit_log()
    assert line["event"] == "blocked"
    assert line["guard"] == "namespace"


def test_a_guard_that_refuses_at_click_time_leaves_approved_then_blocked(
    audit_log, monkeypatch
):
    """The whole point, in one test: the proposal was legal when made and illegal by the time it
    was approved, and the audit log says so in that order.
    """
    executed: list[dict] = []

    def counted(apis, **args):
        executed.append(args)
        return args

    spec = REGISTRY["scale_deployment"]
    monkeypatch.setitem(
        approvals.REGISTRY, "scale_deployment", replace(spec, fn=counted)
    )

    # Legal when proposed: propose() reads no cluster, and 4 was an increase from 2.
    proposal = approvals.propose(
        diagnosis({"tool": "scale_deployment", "args": SCALE}), {}
    )
    assert proposal is not None

    # Someone scaled it to 8 while the message sat unread.
    monkeypatch.setattr(approvals, "_apis_for", lambda proposal: apis_with_replicas(8))
    outcome = approvals.decide(proposal.id, "approve", "aniket")

    assert not outcome.ok
    assert "live_replicas" in outcome.message
    assert executed == []
    assert proposal.state == approvals.BLOCKED
    assert [e["event"] for e in audit_log()] == ["proposed", "approved", "blocked"]


def test_a_blocked_proposal_cannot_be_clicked_again(audit_log):
    """BLOCKED is terminal, like EXECUTED and FAILED. A refused action gets a fresh
    diagnosis, not a second click on a stale button."""
    proposal = approvals.propose(
        diagnosis({"tool": "scale_deployment", "args": SCALE}), {}
    )
    proposal.state = approvals.BLOCKED

    outcome = approvals.decide(proposal.id, "approve", "aniket")

    assert not outcome.ok
    assert "blocked" in outcome.message


# --- drift guards -----------------------------------------------------------------


def test_the_event_names_this_reads_are_the_ones_approvals_writes():
    """guardrails.py hardcodes two audit event names rather than importing them. Rename a state
    without this check and every count silently reads zero, which looks like a quiet hour.
    """
    assert guardrails.EXECUTED == approvals.EXECUTED
    assert guardrails.FAILED == approvals.FAILED


def test_the_namespace_allowlist_matches_the_role_it_sits_above():
    """Two files saying the same thing. An allowlist naming a namespace the Role cannot reach
    is a guard that permits what RBAC then 403s.
    """
    import yaml

    rbac = Path(__file__).resolve().parents[1] / "k8s" / "rbac.yaml"
    docs = list(yaml.safe_load_all(rbac.read_text()))
    role = next(doc for doc in docs if doc and doc.get("kind") == "Role")

    assert role["metadata"]["namespace"] in guardrails.NAMESPACES


def test_a_refusal_is_counted_under_the_guard_that_refused(audit_log):
    # The label carries the signal: the bare counter says only that the agent said no,
    # `guard="breaker"` says something is broken and `guard="namespace"` says the model
    # aimed at the wrong cluster. Different pages.
    from conftest import metric

    before = metric("sha_guardrail_blocks_total", guard="namespace")

    with pytest.raises(GuardrailViolation):
        guardrails.check("restart_pod", {"namespace": "kube-system", "pod": "etcd-0"})

    assert metric("sha_guardrail_blocks_total", guard="namespace") == before + 1


def test_the_llm_budget_refusal_is_counted_too(audit_log, monkeypatch):
    # The only guard that never goes through check(), so it is the one a single
    # instrumentation point in the wrong place would miss.
    from conftest import metric

    monkeypatch.setattr(guardrails, "MAX_LLM_CALLS", 1)
    before = metric("sha_guardrail_blocks_total", guard="llm_calls")
    guardrails.check_llm_call()

    with pytest.raises(GuardrailViolation):
        guardrails.check_llm_call()

    assert metric("sha_guardrail_blocks_total", guard="llm_calls") == before + 1
