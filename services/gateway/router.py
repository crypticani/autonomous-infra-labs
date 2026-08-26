"""Which service answers this question -- and nothing else.

The one decision this module makes is a service name. It does not decide whether the
request can proceed: that depends on what the caller attached, which is a dict lookup in
app.py against `Backend.needs` and is therefore something a model cannot get wrong. Keeping
the two apart is the point. A router that also judged feasibility would have two ways to be
wrong and one field to report them in.

So there are exactly two kinds of refusal in the gateway, and only the first is a model's:

    "I don't know which of these you want"   <- here, as service "none"
    "I know, but you didn't send the data"   <- app.py, from the table

Day 23's lesson, one layer up: a model that cannot decline will satisfy every guard and
still be wrong. `needs_human` is what made triage's confident answers worth reading, and
"none" is the same escape hatch for routing.
"""

import json
import logging
import os
import time
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

import backends
import metrics
from errors import GatewayProviderError
from provider import get_router_provider

logger = logging.getLogger(__name__)

# The sentinel the model emits for "I cannot place this". A member of the enum rather than
# a nullable field: `Literal[...] | None` compiles to a JSON-schema anyOf, and anyOf is the
# part of grammar-constrained decoding least likely to survive a backend change. A flat
# five-value enum is one `"enum": [...]` that both Ollama and Gemini handle identically.
NONE = "none"

# Built from backends.BACKENDS, so a fifth service cannot be added to the table without the
# model being allowed to name it -- and cannot be named by the model unless it is in the
# table. The schema is the guard: an invented service name is not validated away after the
# fact, it is unrepresentable.
ServiceName = Literal[backends.NAMES + (NONE,)]

# Below this the gateway declines whatever the model named. A backstop for the failure mode
# a confidence field exists to catch: a model that picks something rather than nothing.
# 0.6, matching the copilot's SIMILARITY_FLOOR in spirit -- both are "refuse rather than
# serve a bad answer", and both are a number a measurement is allowed to move.
MIN_CONFIDENCE = float(os.getenv("GW_MIN_CONFIDENCE", "0.6"))

# How much of the attachment the router is shown. An attachment is real evidence -- a JSON
# alert is almost certainly the agent's, a scan envelope almost certainly triage's -- and
# the first couple of hundred characters carry all of that signal. Sending a 16 MiB scan
# envelope to a classifier would be paying LLM prices to reread what a dict lookup knows.
ATTACHMENT_PREVIEW = int(os.getenv("GW_ATTACHMENT_PREVIEW", "200"))


class Route(BaseModel):
    """The router's whole output.

    Field order is behaviour, not formatting. Generation is left to right, so `reason`
    first means the service name is produced after the sentence explaining it -- the
    explanation is a premise. Reverse the two and the model picks first and writes
    whatever justifies the pick, which reads identically and is worth nothing.
    """

    # max_length is enforced, not requested. Day 26 asked the prompt for "one short
    # sentence" and shipped 270-character paragraphs for a month.
    reason: str = Field(
        max_length=200,
        description="One short sentence: what in the question decides this.",
    )
    service: ServiceName = Field(
        description=f"The service that can answer it, or {NONE!r} if you cannot tell."
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="How sure you are of the service, 0.0 to 1.0."
    )


SYSTEM_PROMPT = f"""\
You route one operational question to the service that can answer it, or to none of them.

The services:
{backends.catalogue()}

Rules:
- Pick the service whose description covers what is being asked for.
- Answer {NONE!r} when the question fits two services equally well, fits none of them, or \
is too vague to place. {NONE!r} is a real answer, and a wrong route wastes more of the \
caller's time than a decline does.
- Do not pick a service because the question repeats a word from its description. "The \
logs say the deployment is unhealthy" is a log question if it asks what the log means, and \
a cluster question if it asks what to do about the deployment.
- Whether the caller supplied the data a service needs is not your problem. Route on what \
is being asked; the gateway tells them what to attach.
- Write `reason` first, as one short sentence naming what in the question decided it. It is \
your reasoning, not a defence of a choice you have already made.
- `confidence` is how sure you are of the service. Report it honestly: the gateway declines \
a low-confidence route rather than guessing, which is what you want when you are unsure.\
"""


def build_user_prompt(question: str, attachment: str = "") -> str:
    """The question, plus a glimpse of whatever came with it."""
    parts = [f"<question>\n{question.strip()}\n</question>"]
    if attachment:
        preview = attachment[:ATTACHMENT_PREVIEW]
        truncated = " (truncated)" if len(attachment) > ATTACHMENT_PREVIEW else ""
        parts.append(
            f'<attachment note="the caller attached data; first '
            f'{ATTACHMENT_PREVIEW} characters{truncated}">\n{preview}\n</attachment>'
        )
    return "\n\n".join(parts)


def _bad_fields(error: ValidationError) -> str:
    """Which field, what it said, and why that was rejected.

    Written after the first measured run, where a 1.5b model answered `"confidence": 10`
    against a schema declaring `le=1.0` -- four times. The old message said "1 field(s)
    bad" and I had to go read the raw-body log line to learn which field and what value,
    which is the wrong amount of work for the one error this path exists to explain.

    It is also the finding worth keeping: grammar-constrained decoding enforces *structure*
    -- `service` never once left its enum across 48 calls -- and does not enforce numeric
    bounds, so `ge`/`le` are a validation after the fact rather than a guard. A 502 is the
    correct outcome and this is what makes it diagnosable.
    """
    parts = []
    for err in error.errors():
        where = ".".join(str(p) for p in err["loc"]) or "(root)"
        # Truncated: a rejected `reason` can be the whole over-long string.
        got = repr(err.get("input"))
        parts.append(f"{where}={got[:80]} ({err['msg']})")
    return "; ".join(parts)


def classify(question: str, attachment: str = "") -> Route:
    """One model call. Raises GatewayProviderError; never returns a guess.

    A response that fails validation becomes a 502 rather than a fallback route. There is
    no safe default service to fall back to -- picking one would be the exact behaviour
    `none` exists to prevent, decided by a bug instead of by the model.
    """
    provider = get_router_provider()
    started = time.perf_counter()
    raw = provider.generate(
        SYSTEM_PROMPT, build_user_prompt(question, attachment), Route
    )
    metrics.ROUTER_DURATION.observe(time.perf_counter() - started)

    try:
        route = Route.model_validate_json(raw)
    except ValidationError as e:
        # Logged raw and truncated: the whole point of a schema-constrained call is that
        # this should be impossible, so when it happens the body is the evidence.
        logger.error(f"the router returned unusable JSON: {raw[:400]!r}")
        raise GatewayProviderError(
            f"the router model returned an invalid route: {_bad_fields(e)}",
            502,
            provider=provider.name,
        ) from e
    except json.JSONDecodeError as e:
        logger.error(f"the router returned non-JSON: {raw[:400]!r}")
        raise GatewayProviderError(
            "the router model returned text that is not JSON",
            502,
            provider=provider.name,
        ) from e

    metrics.CONFIDENCE.observe(route.confidence)
    logger.info(
        f"routed to {route.service} at {route.confidence:.2f}: {route.reason!r}"
    )
    return route


def decision(route: Route) -> tuple[str | None, str | None]:
    """The route as the gateway acts on it: a service name, or None and why not.

    Two return values because they are two different sentences. `route.reason` is the
    model's, and it travels on every response including the successful ones -- that is what
    makes a misroute legible in the answer rather than hidden behind it. The string here is
    the *gateway's*, and it exists only when the gateway overrode or could not use the
    route. Collapsing them into one field would mean a caller could not tell the model's
    account of a decision from the code's.

    Pure -- no model, no clock, no network -- because the confidence floor is exactly the
    kind of rule that gets changed and wants a test that runs in microseconds.
    """
    if route.service == NONE:
        return None, "the router could not place this question against any of the four"
    if route.confidence < MIN_CONFIDENCE:
        return None, (
            f"the router suggested {route.service} but only at {route.confidence:.2f} "
            f"confidence, below the {MIN_CONFIDENCE} floor, so the gateway declined "
            f"rather than guess"
        )
    return route.service, None
