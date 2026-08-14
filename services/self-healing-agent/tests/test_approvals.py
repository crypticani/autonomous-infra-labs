"""Day 18: the gate. Every test here asks one of two questions -- did the cluster get
touched, and does the audit log say so honestly.

No test reaches Slack or a cluster. `spy_tool` swaps a ToolSpec's `fn` for a counter,
which is the only substitution needed: the registry is data, so a fake write tool is a
`dataclasses.replace` rather than a mock framework.
"""

import json
import time
from dataclasses import replace

import pytest

import approvals
import audit
from agent import Diagnosis
from errors import K8sError

RESTART = {
    "tool": "restart_pod",
    "args": {"namespace": "sandbox", "pod": "checkout-api-7d9f-x2k"},
}


def diagnosis(action) -> Diagnosis:
    return Diagnosis(
        summary="checkout-api OOMKilled after the 14:02 deploy raised heap use",
        evidence=("memory limit is 128Mi", "last restart 4m ago"),
        proposed_action=action,
        confidence=0.91,
        incomplete=False,
    )


@pytest.fixture
def audit_log(tmp_path, monkeypatch):
    """Reads back what actually landed on disk, rather than what a mock was told.

    audit.record writes and fsyncs, so by the time a call returns the line is readable
    here -- which is the property the fail-before-acting test depends on.
    """
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(path))

    def events() -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    return events


@pytest.fixture(autouse=True)
def isolated_proposals(monkeypatch):
    """_proposals is module-level state. Without this, a test could pass because of a
    proposal another test left behind, and the double-click test could pass for the
    wrong reason.

    get_apis is stubbed for the same reason test_agent.py stubs it: restart_pod's spec
    declares `needs`, so _execute fetches a client before calling the tool, and without
    a kubeconfig that raises before the fake tool is ever reached -- which would make
    these tests pass or fail on kubeconfig loading rather than on the gate.
    """
    approvals._proposals.clear()
    monkeypatch.setattr(approvals, "slack_enabled", lambda: False)
    monkeypatch.setattr(approvals.k8s_client, "get_apis", lambda: (None, None))
    yield
    approvals._proposals.clear()


@pytest.fixture
def spy_tool(monkeypatch):
    """Counts executions of restart_pod. In half these tests the assertion that matters
    is that this list has length 0 or 1 -- never 2."""
    calls: list[dict] = []

    def counted(apis, **args):
        calls.append(args)
        return {"deleted": args.get("pod")}

    spec = approvals.REGISTRY["restart_pod"]
    monkeypatch.setitem(approvals.REGISTRY, "restart_pod", replace(spec, fn=counted))
    return calls


@pytest.mark.parametrize(
    "action",
    [
        None,
        "just restart the pod",  # prose, not an object
        {"tool": "get_pod_logs", "args": {}},  # read-only: nothing to approve
        {"tool": "rm_minus_rf", "args": {}},  # not a tool at all
        {"tool": "restart_pod", "args": "sandbox/checkout"},  # args not an object
    ],
)
def test_only_a_real_write_tool_call_becomes_a_proposal(action, audit_log):
    """proposed_action's schema is {"object", "null"} -- the model can put anything
    there. Whatever it puts, only a write tool with a dict of args gets a button."""
    assert approvals.propose(diagnosis(action), {}) is None
    assert approvals._proposals == {}
    assert audit_log() == []


def test_approve_executes_once_and_a_second_click_executes_nothing(spy_tool, audit_log):
    """The double-click. Two people seeing the same alert click Approve; the pod is
    deleted once. decide() flips state before executing, so the second call finds a
    proposal that is no longer `proposed`."""
    proposal = approvals.propose(diagnosis(RESTART), {})

    first = approvals.decide(proposal.id, "approve", "aniket")
    second = approvals.decide(proposal.id, "approve", "someone-else")

    assert first.ok
    assert not second.ok
    assert "executed" in second.message
    assert len(spy_tool) == 1
    assert [e["event"] for e in audit_log()] == ["proposed", "approved", "executed"]


def test_reject_never_touches_the_cluster(spy_tool, audit_log):
    proposal = approvals.propose(diagnosis(RESTART), {})

    outcome = approvals.decide(proposal.id, "reject", "aniket")

    assert outcome.ok
    assert spy_tool == []
    assert [e["event"] for e in audit_log()] == ["proposed", "rejected"]


def test_an_expired_proposal_cannot_be_approved(spy_tool, audit_log):
    """Approval clicked the next morning. Expiry is checked before state, so the answer
    is `expired` and not a restart against a cluster that has moved on."""
    proposal = approvals.propose(diagnosis(RESTART), {})
    next_morning = time.time() + approvals.PROPOSAL_TTL + 1

    outcome = approvals.decide(proposal.id, "approve", "aniket", now=next_morning)

    assert not outcome.ok
    assert spy_tool == []
    assert [e["event"] for e in audit_log()] == ["proposed", "expired"]


def test_an_unknown_id_is_refused_rather_than_raising(spy_tool, audit_log):
    """What a click on yesterday's message looks like after a restart: the audit log
    still has the proposal, `_proposals` does not."""
    outcome = approvals.decide("deadbeefcafe", "approve", "aniket")

    assert not outcome.ok
    assert spy_tool == []
    assert audit_log() == []


def test_the_decision_is_on_disk_before_the_action_that_failed(monkeypatch, audit_log):
    """The reason audit.py fsyncs.

    A tool that raises must still leave `approved` behind. Were the record written
    after the call instead, a crash mid-write and a call that never happened would be
    indistinguishable afterwards -- and this is the test that would go green anyway.
    """

    def explode(apis, **args):
        raise K8sError("pod 'checkout-api-7d9f-x2k' not found", status=404)

    spec = approvals.REGISTRY["restart_pod"]
    monkeypatch.setitem(approvals.REGISTRY, "restart_pod", replace(spec, fn=explode))

    proposal = approvals.propose(diagnosis(RESTART), {})
    outcome = approvals.decide(proposal.id, "approve", "aniket")

    assert not outcome.ok
    assert "not found" in outcome.message
    assert [e["event"] for e in audit_log()] == ["proposed", "approved", "failed"]


def test_a_failed_execution_is_not_retryable_by_clicking_again(spy_tool, audit_log):
    """FAILED is terminal, like EXECUTED. A tool that failed once gets a fresh
    diagnosis, not a second click on a stale button."""
    proposal = approvals.propose(diagnosis(RESTART), {})
    proposal.state = approvals.FAILED

    outcome = approvals.decide(proposal.id, "approve", "aniket")

    assert not outcome.ok
    assert spy_tool == []


def test_the_audit_line_carries_the_arguments_that_will_run(audit_log, spy_tool):
    """`approved` records args, not just the tool name: "who approved a restart" is not
    a useful answer without "of what"."""
    proposal = approvals.propose(diagnosis(RESTART), {"labels": {"alertname": "OOM"}})
    approvals.decide(proposal.id, "approve", "aniket")

    approved = [e for e in audit_log() if e["event"] == "approved"][0]
    assert approved["args"] == RESTART["args"]
    assert approved["user"] == "aniket"
    assert approved["id"] == proposal.id
