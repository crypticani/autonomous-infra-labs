"""The diagnosis loop.

Safety does not live here -- the allowlist check below is what holds. Termination is a
tool call, `submit_diagnosis`, intercepted by name before generic dispatch: a model
narrating in prose instead is not an answer this loop can use, so the transcript grows
by a turn and it tries again. MAX_ITERATIONS stops that running forever without ever
inventing a confidence number to fill the gap.
"""

import logging
import os
import time
from dataclasses import dataclass

import guardrails
import k8s_client
import metrics
from errors import GuardrailViolation, UpstreamError
from provider import BaseAgentProvider
from tools import READ_ONLY, REGISTRY, as_model_tools

logger = logging.getLogger(__name__)

# Raised from 6: a live diagnosis spent all six turns on tool calls and never reached
# submit_diagnosis. One spare turn is a rounding error -- a single retry consumes it,
# and two of the four read-only tools fail whenever no cluster is reachable.
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
    """What the loop produced -- or did not. `incomplete` is the field a caller must check
    first: confidence 0.0 and confidence None both mean "do not trust this", but only one
    means the model finished.
    """

    summary: str | None
    evidence: tuple[str, ...]
    proposed_action: dict | None
    confidence: float | None
    incomplete: bool


def _dispatch(name: str, args: dict) -> dict:
    """Runs one tool, uniformly. Returns `{"output": ...}` or `{"error": ...}`, so whatever a
    tool raises becomes a message the model can route around rather than an exception that
    ends the diagnosis.
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
    """The loop, counted and timed, so every way a diagnosis can end is recorded in one place.

    The four outcomes are not interchangeable: `incomplete` is a returned Diagnosis, so one
    counter would call it a success and hide the only failure here that raises nothing.
    `blocked` is a working guardrail, `failed` is a page.

    Timing is in `finally` because failures cost real seconds too.
    """
    started = time.monotonic()
    try:
        result = _loop(alert, provider, allowed)
    except GuardrailViolation:
        metrics.DIAGNOSES.labels(outcome="blocked").inc()
        raise
    except UpstreamError:
        metrics.DIAGNOSES.labels(outcome="failed").inc()
        raise
    else:
        metrics.DIAGNOSES.labels(
            outcome="incomplete" if result.incomplete else "complete"
        ).inc()
        return result
    finally:
        metrics.DIAGNOSIS_DURATION.observe(time.monotonic() - started)


def _loop(
    alert: dict,
    provider: BaseAgentProvider,
    allowed: tuple[str, ...] = READ_ONLY,
) -> Diagnosis:
    contents = [provider.user(f"Alert:\n{alert}")]
    tools = as_model_tools(allowed)

    for iteration in range(1, MAX_ITERATIONS + 1):
        # Here rather than in provider.chat(), so no backend has to remember to ask.
        # MAX_ITERATIONS caps one diagnosis; this caps all of them.
        guardrails.check_llm_call()
        turn = provider.chat(SYSTEM_PROMPT, contents, tools, allowed=allowed)
        contents.append(turn.raw)
        # Names, not a count: when this exhausts MAX_ITERATIONS the only useful question is what
        # it spent the turns on, and a count cannot answer it.
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
