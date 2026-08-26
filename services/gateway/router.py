"""Which service answers this question -- and nothing else.

Feasibility is app.py's job, against `Backend.needs`. See Readme.md for why the two are
separate.
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

# An enum member rather than a nullable field: `Literal[...] | None` becomes a JSON-schema
# anyOf, which grammar-constrained decoding handles least reliably.
NONE = "none"

# Built from the table, so the model can name exactly the services that exist.
ServiceName = Literal[backends.NAMES + (NONE,)]

# An enum, not a float: a grammar enforces membership but not numeric bounds, so `ge/le`
# was a post-hoc rejection and both models tripped it. Do not change back.
LEVELS: tuple[str, ...] = ("low", "medium", "high")
Confidence = Literal[LEVELS]


def _validated_level(value: str) -> str:
    """Extracted so a test can reach it without reloading this module mid-import."""
    level = value.strip().lower()
    if level not in LEVELS:
        raise ValueError(f"GW_ROUTE_ON must be one of {LEVELS}, got {value!r}")
    return level


# The lowest level the gateway acts on. `medium` preserves the 0.6-of-1.0 floor it replaced.
ROUTE_ON = _validated_level(os.getenv("GW_ROUTE_ON", "medium"))

# Enough of the attachment to be evidence, not enough to pay LLM prices for.
ATTACHMENT_PREVIEW = int(os.getenv("GW_ATTACHMENT_PREVIEW", "200"))


class Route(BaseModel):
    """Field order is behaviour: `reason` before `service` conditions the pick on it."""

    reason: str = Field(
        max_length=200,
        description="One short sentence: what in the question decides this.",
    )
    service: ServiceName = Field(
        description=f"The service that can answer it, or {NONE!r} if you cannot tell."
    )
    confidence: Confidence = Field(
        description="How sure you are of the service: high, medium or low."
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
- `confidence` is how sure you are of the service: `high` if the question plainly belongs \
there, `medium` if it probably does, `low` if you are guessing. Report it honestly -- the \
gateway declines a route you are not confident in rather than passing the guess along, which \
is what you want when you are unsure.\
"""


def build_user_prompt(question: str, attachment: str = "") -> str:
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
    """Which field, what it sent, why it was rejected."""
    parts = []
    for err in error.errors():
        where = ".".join(str(p) for p in err["loc"]) or "(root)"
        got = repr(err.get("input"))
        parts.append(f"{where}={got[:80]} ({err['msg']})")
    return "; ".join(parts)


def classify(question: str, attachment: str = "") -> Route:
    """One model call. Raises GatewayProviderError rather than falling back to a route."""
    provider = get_router_provider()
    started = time.perf_counter()
    raw = provider.generate(
        SYSTEM_PROMPT, build_user_prompt(question, attachment), Route
    )
    metrics.ROUTER_DURATION.observe(time.perf_counter() - started)

    try:
        route = Route.model_validate_json(raw)
    except ValidationError as e:
        # A schema-constrained call should make this impossible, so log the body.
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

    metrics.CONFIDENCE.labels(level=route.confidence).inc()
    logger.info(
        f"routed to {route.service} at {route.confidence} confidence: {route.reason!r}"
    )
    return route


def decision(route: Route) -> tuple[str | None, str | None]:
    """A service to call, or None and the gateway's own reason for declining.

    Separate from `route.reason`, which is the model's and travels on every response.
    """
    if route.service == NONE:
        return None, "the router could not place this question against any of the four"
    # By position, not by string: "medium" < "high" is false alphabetically.
    if LEVELS.index(route.confidence) < LEVELS.index(ROUTE_ON):
        return None, (
            f"the router suggested {route.service} but only at {route.confidence} "
            f"confidence, under the {ROUTE_ON} floor, so the gateway declined rather "
            f"than guess"
        )
    return route.service, None
