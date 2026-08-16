"""The approval gate -- Day 18. A write tool runs from exactly one place: decide().

agent.py still passes READ_ONLY and cannot reach a write. This module deliberately
does not import agent._dispatch either, so there is no import edge at all from the
reasoning loop to a cluster mutation -- the three lines it costs to re-dispatch here
are cheaper than an edge someone has to reason about later.

Proposals live in memory and die with the process. That is not a gap. A proposal
reasoned about a cluster state, and one that survived a restart is consent for a
cluster that may no longer exist; audit.jsonl keeps the record, _proposals keeps only
what is still actionable.
"""

import logging
import os
import time
import uuid
from dataclasses import dataclass

import audit
import guardrails
import k8s_client
import metrics
import slack
from errors import GuardrailViolation
from tools import REGISTRY

logger = logging.getLogger(__name__)

# An approval clicked the next morning is not consent for the cluster as it is now.
PROPOSAL_TTL = int(os.getenv("SHA_PROPOSAL_TTL", "3600"))
APPROVAL_CHANNEL = os.getenv("SHA_APPROVAL_CHANNEL", "")

PROPOSED = "proposed"
APPROVED = "approved"
EXECUTED = "executed"
REJECTED = "rejected"
EXPIRED = "expired"
FAILED = "failed"
# Day 19. Not FAILED: nothing broke, the agent refused. Terminal like the other two, so a
# blocked proposal cannot be clicked into a second attempt.
BLOCKED = "blocked"


@dataclass
class Proposal:
    id: str
    tool: str
    args: dict
    summary: str
    confidence: float | None
    created: float
    state: str = PROPOSED


@dataclass(frozen=True)
class Decision:
    """What to tell Slack. `ok` is whether a transition actually happened -- a
    double-click and a real approval both return a Decision, and only one of them ran
    anything."""

    ok: bool
    message: str


_proposals: dict[str, Proposal] = {}


def slack_enabled() -> bool:
    """Indirected through this module so a test can silence the post without patching
    slack's own predicate out from under its unit tests."""
    return slack.slack_active() and bool(APPROVAL_CHANNEL)


def _validate(action) -> tuple[str, dict] | None:
    """The model's proposed_action is free-form: its schema is {"object", "null"}.

    Anything that is not a real write tool with a dict of arguments dies here rather
    than becoming a button a human can click. A read-only tool is refused too -- there
    is nothing to approve about reading a log, and offering it would train the on-call
    to click Approve without reading.
    """
    if not isinstance(action, dict):
        return None
    tool = action.get("tool")
    args = action.get("args", {})
    spec = REGISTRY.get(tool) if isinstance(tool, str) else None
    if spec is None or not spec.write or not isinstance(args, dict):
        return None
    # The tool's own schema says what it needs. Without this a proposal missing an
    # argument still becomes a button, and the click reaches _execute() and dies on a
    # TypeError -- audited as FAILED, indistinguishable from a cluster that refused.
    if any(field not in args for field in spec.schema.get("required", [])):
        return None
    return tool, args


def propose(diagnosis, alert: dict, now: float | None = None) -> Proposal | None:
    """Records and posts one proposal, or returns None when there is nothing to approve.

    Audit first, post second: a proposal Slack never received still has to exist in the
    record, or a failed post looks identical to a diagnosis that proposed nothing.
    """
    validated = _validate(diagnosis.proposed_action)
    if validated is None:
        if diagnosis.proposed_action is not None:
            logger.warning(f"refused proposed_action {diagnosis.proposed_action!r}")
        return None

    tool, args = validated
    # Before the proposal exists, so a refused action never becomes a button at all. The
    # cluster is not read here (no `apis`): the model has just finished reading it, and the
    # check that needs live state is the one decide() runs at click time.
    try:
        guardrails.check(tool, args)
    except GuardrailViolation as e:
        audit.record(BLOCKED, tool=tool, args=args, guard=e.guard, reason=str(e))
        metrics.PROPOSALS.labels(state=BLOCKED).inc()
        logger.warning(f"guardrail {e.guard!r} refused {tool}: {e}")
        return None

    proposal = Proposal(
        id=uuid.uuid4().hex[:12],
        tool=tool,
        args=args,
        summary=diagnosis.summary or "",
        confidence=diagnosis.confidence,
        created=time.time() if now is None else now,
    )
    audit.record(
        PROPOSED,
        id=proposal.id,
        tool=tool,
        args=args,
        confidence=proposal.confidence,
        alert=alert.get("labels", alert),
    )
    _proposals[proposal.id] = proposal
    metrics.PROPOSALS.labels(state=PROPOSED).inc()

    if slack_enabled():
        slack.post_blocks(APPROVAL_CHANNEL, slack.blocks_for(proposal))
    else:
        logger.info(f"slack inactive: proposal {proposal.id} recorded, not posted")
    return proposal


def _sweep(now: float) -> None:
    """Expiry is a decision too, and gets a line. A proposal that quietly vanished
    would leave the audit log showing one that was never resolved."""
    for proposal in list(_proposals.values()):
        if now - proposal.created > PROPOSAL_TTL:
            if proposal.state == PROPOSED:
                audit.record(EXPIRED, id=proposal.id, tool=proposal.tool)
                metrics.PROPOSALS.labels(state=EXPIRED).inc()
            del _proposals[proposal.id]


def _apis_for(proposal: Proposal):
    """Fetched once and handed to both the guardrail and the tool. get_apis is lru_cached
    so this is not about cost -- it is that the check and the write have to be looking at
    the same cluster."""
    spec = REGISTRY[proposal.tool]
    return k8s_client.get_apis() if spec.needs else (None, None)


def _execute(proposal: Proposal, apis) -> dict:
    """Deliberately not agent._dispatch. That function wraps every failure into a dict
    for the model to read; here a failure has to reach decide() as an exception, so the
    audit line says FAILED instead of recording a success with an error inside it."""
    return REGISTRY[proposal.tool].fn(apis, **proposal.args)


def decide(
    proposal_id: str, decision: str, user: str, now: float | None = None
) -> Decision:
    """The one place a write tool can run.

    The order is not rearrangeable: unknown, then expired, then already-decided, then
    act. Expiry is checked before state so a stale proposal cannot be approved, and the
    state flips to APPROVED *before* execution, so a second click arriving while the
    first is still talking to the API server sees a non-proposed state and refuses.
    That check-and-set is the whole defence against one alert restarting a pod twice.
    """
    now = time.time() if now is None else now
    _sweep(now)

    proposal = _proposals.get(proposal_id)
    if proposal is None:
        # Also the post-restart case: the record is in audit.jsonl, the proposal is not.
        return Decision(False, f"Proposal `{proposal_id}` is expired or unknown.")
    if proposal.state != PROPOSED:
        return Decision(False, f"Already {proposal.state} — nothing further to do.")

    if decision != "approve":
        proposal.state = REJECTED
        audit.record(REJECTED, id=proposal.id, tool=proposal.tool, user=user)
        metrics.PROPOSALS.labels(state=REJECTED).inc()
        return Decision(True, f"Rejected by {user}. Nothing was changed.")

    proposal.state = APPROVED
    audit.record(
        APPROVED, id=proposal.id, tool=proposal.tool, args=proposal.args, user=user
    )
    metrics.PROPOSALS.labels(state=APPROVED).inc()

    # Day 19: the second checkpoint, and the one that matters. It sits after the state
    # flip so the double-click defence is untouched, and after the `approved` line so the
    # log reads approved -> blocked -- a human did click yes, and the machine refused
    # anyway. A guard evaluated only at propose time would have said nothing here.
    # One try for both, so that loading a client, refusing, and failing all end up
    # somewhere deliberate. GuardrailViolation is caught first and separately: it is the
    # only one of the three where nothing was attempted.
    try:
        apis = _apis_for(proposal)
        guardrails.check(proposal.tool, proposal.args, apis=apis)
        result = _execute(proposal, apis)
    except GuardrailViolation as e:
        proposal.state = BLOCKED
        audit.record(
            BLOCKED, id=proposal.id, tool=proposal.tool, guard=e.guard, reason=str(e)
        )
        metrics.PROPOSALS.labels(state=BLOCKED).inc()
        logger.warning(
            f"guardrail {e.guard!r} refused {proposal.tool} after approval: {e}"
        )
        return Decision(
            False, f"Approved by {user}, but the `{e.guard}` guardrail refused it: {e}"
        )
    except Exception as e:
        proposal.state = FAILED
        audit.record(FAILED, id=proposal.id, tool=proposal.tool, error=str(e))
        metrics.PROPOSALS.labels(state=FAILED).inc()
        logger.error(f"{proposal.tool} failed after approval by {user}: {e}")
        return Decision(False, f"Approved by {user}, but `{proposal.tool}` failed: {e}")

    proposal.state = EXECUTED
    audit.record(EXECUTED, id=proposal.id, tool=proposal.tool, result=result)
    metrics.PROPOSALS.labels(state=EXECUTED).inc()
    return Decision(True, f"Approved by {user}. Ran `{proposal.tool}`.")
