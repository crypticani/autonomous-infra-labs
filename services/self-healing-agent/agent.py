"""The diagnosis loop -- Day 17. Runs read-only today; Day 18 widens `allowed` to ALL
and puts the approval gate between a proposed write and its execution, not inside this
loop. This module is not where safety lives regardless -- the allowlist check below is.

Termination is a tool call, `submit_diagnosis`, intercepted by name before generic
dispatch. A model narrating in prose instead of calling it is not an answer this loop
can use, so the transcript grows by one turn and the loop tries again; MAX_ITERATIONS is
what stops that from running forever, without ever inventing a confidence number to fill
the gap. See docs/superpowers/specs/2026-08-11-week3-agent-design.md, decisions 3 and 4.
"""

import logging
import os
from dataclasses import dataclass

import guardrails
import k8s_client
from provider import BaseAgentProvider
from tools import READ_ONLY, REGISTRY, as_model_tools

logger = logging.getLogger(__name__)

# Was 6 -- four read-only tools plus submit_diagnosis, one pass with a single turn spare.
# Raised on the evidence that comment asked for: a live HighRequestLatency diagnosis on
# 2026-08-14 spent all six turns on tool calls and never reached submit_diagnosis, so the
# loop returned incomplete. One spare turn is not slack, it is a rounding error -- a single
# retry of a failing tool consumes it, and two of the four read-only tools fail whenever no
# cluster is reachable. 10 leaves room to gather, retry once, and still conclude.
MAX_ITERATIONS = int(os.getenv("SHA_MAX_ITERATIONS", "10"))

SYSTEM_PROMPT = """You are an on-call SRE agent. You will be shown one alert.

Use the tools offered to work out what is wrong: read logs, check recent deploys, check
other firing alerts, search runbooks. Call one tool at a time and read its result before
deciding the next call.

When you have enough evidence, end the diagnosis by calling submit_diagnosis exactly
once, with your summary, the evidence you gathered, an optional proposed_action, and a
confidence score from 0 to 1. Do not guess at a fix without evidence for it.

proposed_action is not free text. It goes to a human as a button that executes exactly
what it says, so it must name one of the action tools you were offered and give that
tool's arguments. If the fix this alert really needs is not among those tools -- an
OOMKill usually wants a higher memory limit, and nothing here can change one -- then
set proposed_action to null and say so in the summary. Proposing a restart because the
field exists is worse than proposing nothing: it puts a real action in front of a
human at 3am that will not fix their problem."""


@dataclass(frozen=True)
class Diagnosis:
    """What the loop produced -- or didn't. `incomplete` is the field a caller must
    check first: a confidence of 0.0 and a confidence of None both mean "don't trust
    this," but only one of them means the model actually finished."""

    summary: str | None
    evidence: tuple[str, ...]
    proposed_action: dict | None
    confidence: float | None
    incomplete: bool


def _dispatch(name: str, args: dict) -> dict:
    """Runs one tool, uniformly -- `fn(apis, **args)`, whatever the tool actually needs
    `apis` for. Returns `{"output": ...}` or `{"error": ...}`, the convention
    provider.tool_result() expects: whatever a tool raises becomes a message the model
    can read and route around, not an exception that ends the diagnosis.

    `apis` is only fetched for real when `needs` says the tool touches the cluster --
    get_recent_alerts and search_runbooks ignore the argument entirely, and loading a
    kubeconfig just to hand them something they throw away would fail every diagnosis
    on a box with no cluster reachable, for tools that never needed one.
    """
    spec = REGISTRY[name]
    try:
        apis = k8s_client.get_apis() if spec.needs else (None, None)
        result = spec.fn(apis, **args)
    except Exception as e:
        return {"error": str(e)}
    return {"output": result}


def diagnose(
    alert: dict,
    provider: BaseAgentProvider,
    allowed: tuple[str, ...] = READ_ONLY,
) -> Diagnosis:
    contents = [provider.user(f"Alert:\n{alert}")]
    tools = as_model_tools(allowed)

    for iteration in range(1, MAX_ITERATIONS + 1):
        # Here rather than inside provider.chat(): one call site covers every backend, and
        # no provider implementation has to remember to ask. MAX_ITERATIONS is the cap
        # within one diagnosis; this is the cap across all of them, which is the one that
        # matters once Alertmanager is calling /diagnose with nobody watching.
        guardrails.check_llm_call()
        turn = provider.chat(SYSTEM_PROMPT, contents, tools, allowed=allowed)
        contents.append(turn.raw)
        # Names, not just a count. When this loop exhausts MAX_ITERATIONS the only useful
        # question is *what it spent the turns on* -- retrying a dead tool, or asking the
        # runbooks four different questions -- and a count cannot answer either. Learned
        # from an incomplete diagnosis whose logs said "1 tool call" six times over.
        logger.info(
            f"iteration {iteration}/{MAX_ITERATIONS}: "
            f"{[c.name for c in turn.tool_calls] or 'no tool calls'}"
        )

        for tool_call in turn.tool_calls:
            if tool_call.name == "submit_diagnosis":
                return Diagnosis(
                    summary=tool_call.args["summary"],
                    evidence=tuple(tool_call.args["evidence"]),
                    proposed_action=tool_call.args.get("proposed_action"),
                    confidence=tool_call.args["confidence"],
                    incomplete=False,
                )

            if tool_call.name not in allowed:
                logger.warning(f"model asked for {tool_call.name!r}, refused")
                result = {"error": f"{tool_call.name!r} is not an available tool here"}
            else:
                result = _dispatch(tool_call.name, tool_call.args)

            contents.append(provider.tool_result(tool_call, result))

    logger.warning(f"no diagnosis after {MAX_ITERATIONS} iterations")
    return Diagnosis(
        summary=None,
        evidence=(),
        proposed_action=None,
        confidence=None,
        incomplete=True,
    )
