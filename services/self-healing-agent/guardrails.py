"""Day 19. Five refusals, one module.

RBAC is the layer that cannot be argued with: k8s/rbac.yaml grants four verbs in one
namespace, and no bug in this file can widen that. These guards are the layer above it --
they stop actions the Role would happily allow but no on-call would want at 3am, and they
name which rule refused, in a sentence a human reads in Slack.

Every guard runs twice: once in propose(), before a button exists, and again in decide(),
before the write. That is not belt-and-braces. The guards worth having are the ones whose
answer *changes* between those two moments -- another action ran, an execution failed,
someone scaled the Deployment by hand while the message sat unread. A guardrail evaluated
only at propose time protects the cluster as it was, not as it is.

Counts come from audit.jsonl rather than from counters in this process. The log is already
append-only and fsynced; it survives the restart that would otherwise refill an hourly
budget and quietly close an open breaker; and "why is the breaker open" is answerable with
grep instead of a debugger.
"""

import json
import logging
import os
import time

import audit
from errors import GuardrailViolation, UpstreamError
from tools.k8s import MIN_REPLICAS, current_replicas

logger = logging.getLogger(__name__)

# The audit event names this module reads. Literals, not an import from approvals: that
# module imports this one, and a cycle to save two strings is a bad trade.
# test_guardrails.py asserts they still match approvals' constants.
EXECUTED = "executed"
FAILED = "failed"

# Second layer under RBAC, and the one that is readable without `kubectl auth can-i`.
NAMESPACES: tuple[str, ...] = tuple(
    ns.strip() for ns in os.getenv("SHA_NAMESPACES", "sandbox").split(",") if ns.strip()
)

# Three cluster writes an hour is a lot for a sandbox and not enough to be a flapping
# alert's amplifier.
MAX_ACTIONS_PER_HOUR = int(os.getenv("SHA_MAX_ACTIONS_PER_HOUR", "3"))

# Consecutive failed executions before this stops proposing anything at all.
BREAKER_THRESHOLD = int(os.getenv("SHA_BREAKER_THRESHOLD", "3"))

# Not a per-diagnosis cap -- MAX_ITERATIONS is already that. This is the ceiling across
# diagnoses, which is what matters from Day 20 on, when Alertmanager drives /diagnose
# unattended and one flapping alert could spend the whole free tier before breakfast.
MAX_LLM_CALLS = int(os.getenv("SHA_MAX_LLM_CALLS", "30"))

# Shared by the rate limit, the breaker and the model-call budget. The breaker needs a
# time bound or it deadlocks: it blocks the only event that could close it, a successful
# execution. Bounding the window lets it half-open after a quiet hour, which is cheaper
# than a reset mechanism nobody will remember exists.
WINDOW = int(os.getenv("SHA_GUARD_WINDOW", "3600"))


def _events(since: float) -> list[dict]:
    """Audit lines at or after `since`, oldest first.

    A missing log is an empty history, not an error -- the first action after a fresh
    deploy has nothing it could have exceeded. A log that exists and does not parse is a
    different thing entirely, and the JSONDecodeError is deliberately left to propagate:
    a history this cannot read is a history it cannot check, and a guard that cannot
    check must not shrug and allow. Same fail-closed direction as audit.record's missing
    try/except.
    """
    try:
        with open(audit.AUDIT_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    # ponytail: reads the whole file. One line per decision keeps this in kilobytes for
    # months; seek to the tail if it ever stops being true.
    events = [json.loads(line) for line in lines if line.strip()]
    return [e for e in events if e.get("ts", 0) >= since]


def _outcomes(now: float) -> list[str]:
    """Every execution attempt in the window, oldest first: `executed` or `failed`.

    Both stateful guards read this one list, and `failed` counts as an attempt for both.
    A tool that raised still reached the API server, so it spent the hour's budget -- and
    three of them in a row is exactly what the breaker exists to notice.

    Deliberately not `approved`. An approval that a guard then blocked, or one whose
    execution never happened, must not spend the budget: otherwise one bad proposal
    poisons the window and the guards compound each other.
    """
    return [
        e["event"]
        for e in _events(now - WINDOW)
        if e.get("event") in (EXECUTED, FAILED)
    ]


def _check_namespace(args: dict) -> None:
    """Missing is refused as firmly as wrong. `args.get` returning None lands outside any
    allowlist, which is the answer fail-closed wants."""
    namespace = args.get("namespace")
    if namespace not in NAMESPACES:
        raise GuardrailViolation(
            f"namespace {namespace!r} is not in the allowlist {list(NAMESPACES)}",
            guard="namespace",
        )


def _check_replica_floor(args: dict) -> None:
    """Refused, not clamped. tools/k8s.py clamps a model-chosen count so one bad number
    does not throw away a whole diagnosis; here the number is already in front of a human
    as a button, and silently turning "scale to 0" into "scale to 1" would execute
    something nobody approved."""
    replicas = args.get("replicas")
    if not isinstance(replicas, int) or replicas < MIN_REPLICAS:
        raise GuardrailViolation(
            f"scaling to {replicas!r} is below the floor of {MIN_REPLICAS}: a service "
            "scaled to zero is an outage, not a fix",
            guard="replica_floor",
        )


def _check_live_replicas(args: dict, apis) -> None:
    """The check that needed execute time to mean anything.

    An approval clicked twenty minutes later was reasoned about a replica count that may
    have moved since -- possibly by a human responding to the same incident. Scaling
    *down* to a number that was an increase when it was proposed is the failure this
    stops.

    A read that fails is a refusal, not a pass: if the current state cannot be
    established, it cannot be checked against.
    """
    try:
        live = current_replicas(
            apis, namespace=args["namespace"], deployment=args["deployment"]
        )
    except UpstreamError as e:
        raise GuardrailViolation(
            f"could not read the current replica count to check this against: {e}",
            guard="live_replicas",
        ) from e

    if args["replicas"] < live:
        raise GuardrailViolation(
            f"this would scale {args['deployment']} down from {live} to "
            f"{args['replicas']}: the replica count changed after this was proposed",
            guard="live_replicas",
        )


def _check_rate(now: float) -> None:
    attempts = len(_outcomes(now))
    if attempts >= MAX_ACTIONS_PER_HOUR:
        raise GuardrailViolation(
            f"{attempts} actions already executed in the last {WINDOW // 60}m, limit "
            f"is {MAX_ACTIONS_PER_HOUR}: a flapping alert is not sixty restarts",
            guard="rate_limit",
        )


def _check_breaker(now: float) -> None:
    if BREAKER_THRESHOLD < 1:
        # 0 disables the breaker. Spelled out because outcomes[-0:] is the whole list,
        # not the empty one, and that reads as "block everything, forever".
        return
    tail = _outcomes(now)[-BREAKER_THRESHOLD:]
    if len(tail) == BREAKER_THRESHOLD and all(o == FAILED for o in tail):
        raise GuardrailViolation(
            f"the last {BREAKER_THRESHOLD} executions all failed: something is wrong "
            "that one more action will not fix",
            guard="breaker",
        )


def check(tool: str, args: dict, apis=None, now: float | None = None) -> None:
    """Raises GuardrailViolation, or returns nothing.

    Cheapest first, cluster read last: a proposal the namespace allowlist refuses should
    not cost an API call to find out.

    `apis` is the whole difference between the two call sites. propose() passes nothing,
    because the model has just finished reading the cluster; decide() passes the client,
    which turns on the one check that compares the proposal against the cluster as it is
    at the moment of the click.
    """
    now = time.time() if now is None else now

    _check_namespace(args)
    if tool == "scale_deployment":
        _check_replica_floor(args)
    # Breaker before rate limit, and the order is load-bearing: N consecutive failures is
    # also N attempts, so with the two thresholds at the same default the rate limit would
    # answer first every time and the breaker could never fire at all. Where both apply,
    # "something is broken" is the more useful sentence than "you are going too fast".
    _check_breaker(now)
    _check_rate(now)
    if tool == "scale_deployment" and apis is not None:
        _check_live_replicas(args, apis)


# In memory, unlike every other count here, and for a reason: a model call is not an
# audit event. Ten lines per diagnosis would bury the decisions this log exists to record
# under the arithmetic. A restart refills this budget -- the right trade for a guard whose
# job is stopping a runaway loop, not accounting for a quota.
_llm_calls: list[float] = []


def check_llm_call(now: float | None = None) -> None:
    """Records the call as well as checking it: the budget has to include the call it is
    about to allow, or the cap is off by one every window."""
    now = time.time() if now is None else now
    _llm_calls[:] = [t for t in _llm_calls if t >= now - WINDOW]
    if len(_llm_calls) >= MAX_LLM_CALLS:
        raise GuardrailViolation(
            f"{len(_llm_calls)} model calls in the last {WINDOW // 60}m, limit is "
            f"{MAX_LLM_CALLS}",
            guard="llm_calls",
        )
    _llm_calls.append(now)
