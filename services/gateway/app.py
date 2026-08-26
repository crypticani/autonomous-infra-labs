"""FastAPI surface for the gateway.

Four services behind one address, one edge token, and one endpoint that decides which of
them a plain-English question belongs to.

POST /ask always answers 200 when the gateway itself worked, and the `outcome` field is the
contract -- `unroutable` and `needs_input` are results, not errors. That follows the two
services that already do it: triage returns `needs_human` inside a 200, and the copilot
returns `grounded: false` the same way. A backend failure is different and does surface as
the backend's own status code, because nothing about that is a legitimate answer.

GET /s/{service}/{path} is the plain passthrough, and involves no model at all.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

import metrics
import router
from backends import BACKENDS, BY_NAME
from errors import GatewayProviderError
from provider import get_router_provider
from router import classify, decision

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logger = logging.getLogger(__name__)

# Plural, following security-triage: one token per caller, so revoking a leaked one is a
# list edit rather than a rotation everybody has to be told about.
TOKENS = {t.strip() for t in os.getenv("GW_API_TOKENS", "").split(",") if t.strip()}

# 16 MiB, matching security-triage's, because the same scan envelope can arrive here as an
# `attachment` and then be forwarded there. A smaller cap at the edge would make the
# gateway refuse bodies the service behind it accepts.
#
# nginx caps uploads at 1 MiB by default and sits in front of this in the real deploy, so
# it is the binding limit until `client_max_body_size` is raised. See the Readme.
MAX_BODY_BYTES = int(os.getenv("GW_MAX_BODY_BYTES", str(16 * 1024 * 1024)))

# Generous next to triage's 5/hour, because this is a human-facing endpoint rather than a
# CI job spending CPU-minutes. It exists at all because /ask spends a model call *before*
# any backend's own limit gets a say, so without it one token can burn the router freely.
MAX_ASKS_PER_HOUR = int(os.getenv("GW_MAX_ASKS_PER_HOUR", "60"))
RATE_WINDOW = int(os.getenv("GW_RATE_WINDOW", "3600"))

# The /ask forward. 300 because the copilot's own LLM_TIMEOUT is 300 and it is the slowest
# of the four; the rest answer in seconds and triage answers 202 immediately.
BACKEND_TIMEOUT = int(os.getenv("GW_BACKEND_TIMEOUT", "300"))

# Much shorter, and a different number for a different job: /health is what the container's
# HEALTHCHECK and Prometheus both call, so a wedged backend must not hold it open. Four
# fan out concurrently, so this is the whole endpoint's worst case, not four times it.
HEALTH_TIMEOUT = int(os.getenv("GW_HEALTH_TIMEOUT", "5"))

_starts: dict[str, list[float]] = {}

# Four threads for four backends, so /health costs one HEALTH_TIMEOUT rather than four.
# A module-level pool rather than one per request: `with ThreadPoolExecutor(...)` spawns
# and joins four OS threads on every scrape, and Prometheus scrapes forever.
_health_pool = ThreadPoolExecutor(
    max_workers=len(BACKENDS), thread_name_prefix="health"
)


def require_token(request: Request) -> str:
    """Authenticate, and return a non-secret id for the caller."""
    if not TOKENS:
        return "anonymous"

    scheme, _, presented = request.headers.get("Authorization", "").partition(" ")
    if scheme == "Bearer":
        for token in TOKENS:
            if hmac.compare_digest(presented, token):
                return hashlib.sha256(token.encode()).hexdigest()[:8]

    metrics.REFUSALS.labels(reason="auth").inc()
    raise HTTPException(
        status_code=401,
        detail="a valid bearer token is required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def body_size_error(declared: str | None) -> tuple[int, str] | None:
    """On Content-Length, not the parsed body -- by then the memory is already spent."""
    if declared is None:
        return 411, "Content-Length is required on a POST"
    if not declared.isdigit() or int(declared) > MAX_BODY_BYTES:
        return 413, f"the request body must be at most {MAX_BODY_BYTES} bytes"
    return None


def check_rate(caller: str) -> None:
    """One bucket per token, refused with 429."""
    now = time.monotonic()
    recent = [t for t in _starts.get(caller, []) if t > now - RATE_WINDOW]
    if len(recent) >= MAX_ASKS_PER_HOUR:
        metrics.REFUSALS.labels(reason="rate_limit").inc()
        raise HTTPException(
            status_code=429,
            detail=(
                f"{len(recent)} asks already in the last {RATE_WINDOW // 60}m, "
                f"limit is {MAX_ASKS_PER_HOUR}"
            ),
        )
    recent.append(now)
    _starts[caller] = recent


app = FastAPI(
    title="Gateway",
    description="One address for four services, and a router that decides between them",
    version="1.0.0",
)


@app.middleware("http")
async def cap_body_size(request: Request, call_next):
    """Middleware, not `Depends`: FastAPI parses the body before it solves dependencies, so
    a `Depends` guard fires only after the megabytes it exists to refuse are already dicts.
    Returns a response rather than raising -- an HTTPException here is outside FastAPI's
    handlers and 500s.
    """
    if request.method in ("POST", "PUT", "PATCH"):
        error = body_size_error(request.headers.get("content-length"))
        if error:
            status, detail = error
            metrics.REFUSALS.labels(reason="body_size").inc()
            logger.warning(f"refused a body: {detail}")
            return JSONResponse({"detail": detail}, status_code=status)
    return await call_next(request)


class AskRequest(BaseModel):
    question: str = Field(
        min_length=10, description="What you want to know, in English"
    )
    # A string rather than a dict, even for the two backends that want JSON: a raw log is
    # text, and one field that is sometimes text and sometimes an object is a field every
    # caller has to special-case. The two JSON backends parse it, and a parse failure is a
    # 400 that names which.
    attachment: str = Field(
        default="",
        description="The material the chosen service needs: log text, or JSON",
    )


class AskResponse(BaseModel):
    """`service`, `confidence` and `reason` are present on every outcome, successes
    included. That is deliberate: a router you cannot second-guess after the fact is one
    whose mistakes are invisible, and the confident answers are only worth something if a
    wrong route is legible next to them.
    """

    outcome: Literal["answered", "accepted", "needs_input", "unroutable", "failed"]
    service: str | None
    confidence: float
    # The model's sentence.
    reason: str
    # The gateway's, when it has something of its own to say.
    detail: str | None = None
    # Set only on needs_input: the field the caller has to attach.
    needs: str | None = None
    answer: Any = None
    attributed_to: str | None = None
    # security-triage only, which answers 202 and a run id.
    poll: str | None = None


def _reply(outcome: str, route, **extra) -> AskResponse:
    """Every response carries the routing, so it is assembled in one place."""
    metrics.ROUTES.labels(
        service=route.service if route.service != router.NONE else "none",
        outcome=outcome,
    ).inc()
    return AskResponse(
        outcome=outcome,
        service=None if route.service == router.NONE else route.service,
        confidence=route.confidence,
        reason=route.reason,
        **extra,
    )


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest, response: Response, caller: str = Depends(require_token)):
    """Classify, then forward -- or say why not.

    The two refusals here come from different places on purpose. `unroutable` is the
    model's judgment, arriving as service "none" or as a confidence below the floor.
    `needs_input` is a dict lookup against the backend table and involves no model at all,
    which is what makes it the one refusal that cannot be wrong.
    """
    check_rate(caller)

    try:
        route = classify(request.question, request.attachment)
    except GatewayProviderError as e:
        metrics.ROUTES.labels(service="none", outcome="failed").inc()
        logger.error(f"the router itself failed: {e}")
        raise HTTPException(status_code=e.status, detail=f"{e.provider}: {e}")

    name, gateway_note = decision(route)
    if name is None:
        logger.info(f"declined: {gateway_note}")
        return _reply("unroutable", route, detail=gateway_note)

    backend = BY_NAME[name]

    if backend.needs and not request.attachment:
        # The honest end of the plan's own examples: "why did checkout start 500ing at 3am"
        # is log-analyzer's question and log-analyzer analyses text it is handed. Naming
        # the service and refusing beats inventing a log to send it.
        return _reply(
            "needs_input",
            route,
            needs=backend.needs,
            detail=f"{name} can answer this, but {backend.hint}",
        )

    try:
        body = backend.body(request.question, request.attachment)
    except (json.JSONDecodeError, TypeError) as e:
        # Only reachable for the two backends whose attachment is JSON.
        metrics.REFUSALS.labels(reason="bad_attachment").inc()
        raise HTTPException(
            status_code=400,
            detail=f"{name} needs {backend.needs} as valid JSON in `attachment`: {e}",
        )

    target = f"{backend.url()}{backend.path}"
    started = time.perf_counter()
    try:
        upstream = requests.post(
            target, json=body, headers=backend.headers(), timeout=BACKEND_TIMEOUT
        )
    except requests.exceptions.RequestException as e:
        metrics.BACKEND_DURATION.labels(service=name).observe(
            time.perf_counter() - started
        )
        logger.error(f"{name} unreachable at {target}: {e}")
        # The backend's failure, reported with the backend's name on it, and 503 rather
        # than a 200 carrying an apology.
        response.status_code = 503
        return _reply(
            "failed",
            route,
            detail=f"{name} is unreachable: {e}",
            attributed_to=f"{name} POST {backend.path}",
        )
    metrics.BACKEND_DURATION.labels(service=name).observe(time.perf_counter() - started)

    answer = _safe_json(upstream)
    attribution = f"{name} POST {backend.path}"

    if upstream.status_code >= 400:
        logger.warning(f"{name} answered {upstream.status_code}")
        # Mirrored, not flattened to 502: a 422 from the backend means the caller's
        # attachment was wrong and a 429 means it should try later. Those are different
        # instructions and only the real status carries them.
        response.status_code = upstream.status_code
        return _reply(
            "failed",
            route,
            detail=f"{name} answered {upstream.status_code}",
            answer=answer,
            attributed_to=attribution,
        )

    if upstream.status_code == 202:
        # Only security-triage. Calling a pending run "answered" would be a lie the
        # response body immediately contradicts.
        run_id = answer.get("run_id") if isinstance(answer, dict) else None
        return _reply(
            "accepted",
            route,
            answer=answer,
            attributed_to=attribution,
            poll=f"/s/{name}{backend.path}/{run_id}" if run_id else None,
            detail=f"{name} accepted the work; poll for the verdict",
        )

    return _reply("answered", route, answer=answer, attributed_to=attribution)


def _safe_json(upstream: requests.Response) -> Any:
    """A backend's body, whatever shape it came in.

    A 502 page from an intermediary is not JSON, and letting `.json()` raise here would
    turn somebody else's outage into a stack trace in this service's logs.
    """
    try:
        return upstream.json()
    except ValueError:
        return {"raw": upstream.text[:2000]}


@app.api_route(
    "/s/{service}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    dependencies=[Depends(require_token)],
)
def proxy(service: str, path: str, request: Request, body: Any = Body(default=None)):
    """The plain passthrough: one edge token in, each backend's own token out.

    No model, no routing, no rewriting -- this is what a caller uses when it already knows
    which service it wants, which is every CI job. Under /s/ rather than at the root so it
    cannot ever shadow /ask, /health or /metrics; a bare `/{service}/{path}` would depend
    on route declaration order to stay correct.

    `Body(default=None)` is load-bearing and the bare `Any = None` it replaced was a silent
    bug: FastAPI reads an un-annotated `Any` from the query string, so every POST through
    here forwarded an empty body to a backend that answered 422 about a field the caller
    had definitely sent. A GET test passes either way, which is how it survived being
    written.

    ponytail: JSON only, and the whole upstream body lands in memory. Both are true of all
    four backends. Upgrade path is httpx with `stream=True` and the raw content-type
    forwarded rather than re-encoded.
    """
    backend = BY_NAME.get(service)
    if backend is None:
        metrics.REFUSALS.labels(reason="unknown_service").inc()
        raise HTTPException(
            status_code=404,
            detail=f"no service {service!r}; try one of {sorted(BY_NAME)}",
        )

    try:
        upstream = requests.request(
            request.method,
            f"{backend.url()}/{path}",
            json=body,
            params=dict(request.query_params),
            headers=backend.headers(),
            timeout=BACKEND_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        metrics.PROXY.labels(service=service, status="unreachable").inc()
        logger.error(f"proxy to {service} failed: {e}")
        raise HTTPException(status_code=503, detail=f"{service} is unreachable: {e}")

    metrics.PROXY.labels(
        service=service, status=f"{upstream.status_code // 100}xx"
    ).inc()
    return JSONResponse(_safe_json(upstream), status_code=upstream.status_code)


def _backend_health(backend) -> dict[str, Any]:
    """One backend's own /health, or the reason it could not be asked."""
    started = time.perf_counter()
    try:
        upstream = requests.get(f"{backend.url()}/health", timeout=HEALTH_TIMEOUT)
        elapsed = round((time.perf_counter() - started) * 1000)
        body = _safe_json(upstream)
        return {
            "service": backend.name,
            # The backend's own word for it, not a guess from the status code: all four
            # answer 200 while calling themselves degraded, which is the case that
            # matters and the one a status-code check would miss.
            "status": (
                body.get("status", "unknown") if isinstance(body, dict) else "unknown"
            ),
            "http": upstream.status_code,
            "latency_ms": elapsed,
            "issues": body.get("issues", []) if isinstance(body, dict) else [],
        }
    except requests.exceptions.RequestException as e:
        return {
            "service": backend.name,
            "status": "unreachable",
            "http": None,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            # Truncated: a connection error carries the whole URL and retry chain, and
            # this response is read in a terminal.
            "issues": [str(e)[:200]],
        }


@app.get("/health")
def health_check():
    """Aggregated, and unauthenticated so the container's own HEALTHCHECK can run it.

    Reports every backend's own health rather than just this process's liveness -- a
    gateway that says `healthy` while three of the four services behind it are down is
    reporting on the wrong thing.

    Fanned out concurrently: sequentially this would be four HEALTH_TIMEOUTs, which is
    long enough to fail the container healthcheck it exists to serve.
    """
    issues: list[str] = []
    provider_name = model = "unknown"

    try:
        provider = get_router_provider()
        provider_name, model = provider.name, provider.model_name
    except Exception as e:
        provider = None
        issues.append(f"router provider unavailable: {e}")

    # Constructing a provider does no I/O, so without this the endpoint reports a healthy
    # router while the model behind it is unreachable -- and the caller finds out as a 503
    # from /ask instead. Lifted from knowledge-copilot's /health, which has carried the
    # same check since Day 12; log-analyzer's does it too, and this service is the one
    # where it matters most: the laptop hosting Ollama is *expected* to be asleep, so
    # "unreachable model" is a normal state that has to be legible rather than a surprise.
    if provider is not None and provider.name == "ollama":
        try:
            tags = requests.get(f"{provider.base_url}/api/tags", timeout=2)
            tags.raise_for_status()
            pulled = {entry["name"] for entry in tags.json().get("models", [])}
            # Ollama reports `foo:latest` for a model pulled as bare `foo`.
            if model not in pulled and f"{model}:latest" not in pulled:
                issues.append(f"model {model!r} is not pulled on {provider.base_url}")
        except requests.exceptions.RequestException as e:
            issues.append(f"Ollama unreachable at {provider.base_url}: {e}")

    if not TOKENS:
        # A deploy that forgot GW_API_TOKENS is visible here rather than quietly serving
        # every service behind it to the internet.
        issues.append("auth disabled: GW_API_TOKENS is unset")

    downstream = list(_health_pool.map(_backend_health, BACKENDS))
    for entry in downstream:
        if entry["status"] != "healthy":
            issues.append(f"{entry['service']} is {entry['status']}")

    return {
        # Degraded, never unhealthy: /ask still routes and still declines correctly with
        # every backend down, and a container that reports itself dead gets restarted,
        # which fixes nothing when the fault is somebody else's.
        "status": "degraded" if issues else "healthy",
        "provider": provider_name,
        "model": model,
        "auth": f"{len(TOKENS)} token(s)" if TOKENS else "disabled",
        # What this process loaded, not what the image ships: .env overrides image
        # defaults and a stale one has cost an evening before.
        "policy": {
            "min_confidence": router.MIN_CONFIDENCE,
            "max_body_bytes": MAX_BODY_BYTES,
            "max_asks_per_hour": MAX_ASKS_PER_HOUR,
            "window": RATE_WINDOW,
            "backend_timeout": BACKEND_TIMEOUT,
        },
        "backends": downstream,
        "issues": issues,
    }


@app.get("/metrics")
def metrics_endpoint():
    """Prometheus scrape target."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("GW_PORT", "7400")))
