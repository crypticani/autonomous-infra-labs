"""FastAPI surface -- Day 25.

POST /triage answers 202 and a run id; GET /triage/{id} answers `pending` until the
work is done. The same ack-now/answer-later split as Day 13's Slack bot and Day 20's
/alerts, for the same reason and at a worse ratio: a triage run is one model call per
ST_BATCH_SIZE findings, and the committed fixture alone is 559 deduped findings -- over
a hundred calls, minutes each on CPU Ollama. A synchronous endpoint would time out on
every real request, and the caller (a GitHub Actions job) would retry, doubling the
work it just abandoned.

Three controls that exist from the first commit rather than at capstone, because this
is a **public multi-tenant endpoint whose work costs CPU-minutes of somebody else's
inference**:

- bearer auth, ST_API_TOKENS, several accepted so each onboarded repo carries its own,
- a body size cap, because the envelope is arbitrary scanner JSON from the internet
  (this repo's own is 2.7 MB),
- a per-token rate limit, because the cheapest possible denial of service here is a
  valid, well-formed request sent in a loop.

Onboarding a repo is still a token and a URL: none of the three needs per-repo
configuration on this side, and `repo` stays a label rather than a secret.
"""

import hashlib
import hmac
import logging
import os
import time
import uuid
from typing import Any, Literal

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import risk
import triage
from errors import TriageProviderError
from fixes import Fix, propose_fixes
from provider import get_triage_provider
from risk import RiskAssessment, TopFinding, assess, top_findings
from scanners import Finding, dedupe, parse_envelope
from triage import triage_findings

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logger = logging.getLogger(__name__)

# Plural, unlike SHA_API_TOKEN and the copilot's single token: this is the first service
# here with more than one caller by design. One token per onboarded repo means a leaked
# one is revoked by editing a list, and -- the reason it matters below -- the rate limit
# has something per-caller to count against.
TOKENS = {t.strip() for t in os.getenv("ST_API_TOKENS", "").split(",") if t.strip()}

# 16 MiB: this repo's own envelope is 2.7 MB and it is a small repo, so the cap is set
# where a legitimate monorepo still fits and a body meant to exhaust memory does not.
MAX_BODY_BYTES = int(os.getenv("ST_MAX_BODY_BYTES", str(16 * 1024 * 1024)))

MAX_RUNS_PER_HOUR = int(os.getenv("ST_MAX_RUNS_PER_HOUR", "5"))
RATE_WINDOW = int(os.getenv("ST_RATE_WINDOW", "3600"))

# Run records are in-process (see the module note on GET /triage/{id}), so the dict
# needs a ceiling or a long-lived container accumulates every envelope it ever saw.
# Oldest-first eviction, which is dict insertion order.
MAX_RUNS = int(os.getenv("ST_MAX_RUNS", "200"))

_runs: dict[str, "Run"] = {}
_starts: dict[str, list[float]] = {}


def require_token(request: Request) -> str:
    """Authenticate, and return a **non-secret** id for the caller.

    The id is what the rate limit counts against, so it has to survive into logs and
    into the run record -- hence a hash prefix rather than the token itself. Same
    Bearer/compare_digest shape as knowledge-copilot's require_token and the agent's;
    the loop leaks how many tokens are configured through timing, which is not a secret.
    """
    if not TOKENS:
        return "anonymous"

    scheme, _, presented = request.headers.get("Authorization", "").partition(" ")
    if scheme == "Bearer":
        for token in TOKENS:
            if hmac.compare_digest(presented, token):
                return hashlib.sha256(token.encode()).hexdigest()[:8]

    raise HTTPException(
        status_code=401,
        detail="a valid bearer token is required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def body_size_error(declared: str | None) -> tuple[int, str] | None:
    """The cap itself, on Content-Length rather than on the parsed body's length.

    By the time Pydantic has a body the memory is already spent, and this is the one
    check that has to happen before the expensive thing rather than after it. A client
    that under-declares gains nothing: uvicorn's HTTP parser stops reading at the
    declared length, so what the app sees can never exceed what was checked here.

    A chunked request declares no length at all, so there is nothing to check and it is
    refused with 411. Every caller of this endpoint is a `curl --data-binary @file` from
    a CI job, which always sends Content-Length.
    """
    if declared is None:
        return 411, "Content-Length is required on POST /triage"
    if not declared.isdigit() or int(declared) > MAX_BODY_BYTES:
        return 413, f"the scan envelope must be at most {MAX_BODY_BYTES} bytes"
    return None


def check_rate(caller: str) -> None:
    """One bucket per token, refused with 429.

    Not a guardrail against a hostile caller alone -- a repo whose CI retries a failed
    workflow four times would otherwise queue four full triage runs against a backend
    that can serve roughly one. The refusal is the honest answer: the work will not
    happen faster by asking again.
    """
    now = time.monotonic()
    recent = [t for t in _starts.get(caller, []) if t > now - RATE_WINDOW]
    if len(recent) >= MAX_RUNS_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=(
                f"{len(recent)} triage runs already started in the last "
                f"{RATE_WINDOW // 60}m, limit is {MAX_RUNS_PER_HOUR}"
            ),
        )
    recent.append(now)
    _starts[caller] = recent


app = FastAPI(
    title="Security Triage",
    description="Triages scanner findings into one risk score and a CI verdict",
    version="1.0.0",
)


@app.middleware("http")
async def cap_body_size(request: Request, call_next):
    """Middleware and not `Depends`, and the difference is the entire point of the cap.

    FastAPI reads and parses the request body *before* it solves a route's dependencies,
    so a `Depends(...)` guard would fire only after the megabytes it was meant to refuse
    were already read and turned into dicts. Middleware runs before the route is even
    matched, which is the only place the check does what it says.

    It returns a response rather than raising: an HTTPException raised here is outside
    the exception handlers FastAPI installs, and would surface as a 500.
    """
    if request.method == "POST":
        error = body_size_error(request.headers.get("content-length"))
        if error:
            status, detail = error
            logger.warning(f"refused a body: {detail}")
            return JSONResponse({"detail": detail}, status_code=status)
    return await call_next(request)


class TriageRequest(BaseModel):
    """`scan.sh`'s envelope, plus one optional policy field.

    `scans` is a bare dict on purpose: scanners.py is the only module that knows what a
    Trivy or Checkov document looks like, and typing it here would turn every future
    scanner release into a 422 on a body that was perfectly usable.
    """

    repo: str
    commit: str = ""
    branch: str = ""
    scans: dict[str, Any] = Field(default_factory=dict)
    # Each repo sets its own bar. A public API and a cron script have genuinely
    # different thresholds and neither should have to argue with this deploy's default.
    risk_threshold: int | None = Field(default=None, ge=0)


class Run(BaseModel):
    id: str
    status: Literal["pending", "done", "failed"]
    repo: str
    commit: str
    branch: str
    caller: str
    findings_raw: int
    findings: int
    triaged: int = 0
    risk: RiskAssessment | None = None
    # The rows a PR comment renders. The full judgment list is deliberately not here:
    # nothing downstream reads it, and a 559-finding run would make every poll response
    # megabytes of JSON that gets thrown away.
    top: list[TopFinding] = Field(default_factory=list)
    fixes: list[Fix] = Field(default_factory=list)
    error: str | None = None


class TriageAccepted(BaseModel):
    run_id: str
    status: Literal["pending"]
    findings_raw: int
    findings: int


def _remember(run: Run) -> None:
    _runs[run.id] = run
    while len(_runs) > MAX_RUNS:
        del _runs[next(iter(_runs))]


def _triage_in_background(
    run_id: str, raw: list[Finding], deduped: list[Finding], threshold: int | None
) -> None:
    """The run itself, with nobody left to return to -- the 202 went out minutes ago.

    Catches everything, deliberately, for the same reason /alerts does: an escaping
    exception here buys a traceback after the response has already been sent. Worse, it
    would leave the run `pending` forever and the polling CI job would sit there until
    its own timeout with no idea why. A failed run has to be a *recorded* failure.

    `propose_fixes` gets the pre-dedup list and `triage_findings` the deduped one, and
    the asymmetry is Day 24's: nine securityContext rules on one container block share a
    (target, line) fingerprint, which is the right identity for one triage judgment and
    the wrong one for a hunk that needs all nine keys.
    """
    run = _runs.get(run_id)
    if run is None:
        # Only reachable if MAX_RUNS evicted this run between the 202 and the task
        # starting. Without the guard the KeyError lands in the handler below, which
        # then fails on an unbound `run` -- a confusing traceback for a situation with
        # nothing left to record it against anyway.
        logger.warning(f"run {run_id} was evicted before its triage began")
        return

    try:
        results = triage_findings(deduped)
        run.triaged = len(results)
        run.risk = assess(results, threshold=threshold)
        run.top = top_findings(results, deduped)
        run.fixes = propose_fixes(raw)
        run.status = "done"
        logger.info(
            f"run {run_id} done: {run.triaged}/{len(deduped)} triaged, "
            f"score {run.risk.score} -> {run.risk.verdict}"
        )
    except TriageProviderError as e:
        run.status = "failed"
        run.error = f"{e.provider}: {e}"
        logger.error(f"run {run_id} abandoned, {e.provider} failed: {e}")
    except Exception as e:
        run.status = "failed"
        run.error = str(e)
        logger.exception(f"run {run_id} abandoned by an unexpected failure")


@app.post("/triage", status_code=202, response_model=TriageAccepted)
def start_triage(
    request: TriageRequest,
    background: BackgroundTasks,
    caller: str = Depends(require_token),
):
    """202 and a run id. The verdict arrives at GET /triage/{id}.

    Parsing and dedup happen **here**, synchronously, even though they could just as
    easily go in the background task. Both are pure string arithmetic, they take
    milliseconds on a 2.7 MB envelope, and doing them now means a malformed `scans`
    block is a 422 the caller can read rather than a `failed` run it has to poll for.
    The caller also gets the finding count in the ack, which is the only number it can
    use to guess how long to poll.
    """
    check_rate(caller)

    raw = parse_envelope(request.model_dump())
    deduped = dedupe(raw)

    run = Run(
        id=uuid.uuid4().hex[:12],
        status="pending",
        repo=request.repo,
        commit=request.commit,
        branch=request.branch,
        caller=caller,
        findings_raw=len(raw),
        findings=len(deduped),
    )
    _remember(run)
    logger.info(
        f"/triage run {run.id} repo={request.repo!r} commit={request.commit[:8]} "
        f"{len(raw)} findings -> {len(deduped)} deduped"
    )

    background.add_task(
        _triage_in_background, run.id, raw, deduped, request.risk_threshold
    )
    return TriageAccepted(
        run_id=run.id,
        status="pending",
        findings_raw=len(raw),
        findings=len(deduped),
    )


@app.get("/triage/{run_id}", response_model=Run, dependencies=[Depends(require_token)])
def get_run(run_id: str):
    """`pending`, or the verdict.

    The store is a process-local dict, which makes `--workers 1` load-bearing for the
    third time in this repo (the copilot's cache, the agent's proposals, now this) and
    means a container restart mid-run strands a polling CI job on an id that will never
    exist again -- it gets a 404 and fails the job, which is at least the loud version.
    Acceptable at this scale; the upgrade path is a JSON file per run in a bind mount,
    the same shape as the agent's audit log.
    """
    run = _runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
    return run


@app.get("/health")
def health_check():
    """Unauthenticated, like the other two services': a health check behind a bearer
    token is one the container's own HEALTHCHECK cannot run.

    The model backend is named here, not probed. Unlike the agent's cluster check, a
    probe would mean an HTTP call to Ollama every thirty seconds forever, and it would
    not buy much: a dead backend surfaces as `status: failed` with the provider's own
    error on the run record, which is where whoever is waiting is already looking.
    """
    issues: list[str] = []
    provider_name = model = "unknown"

    try:
        provider = get_triage_provider()
        provider_name, model = provider.name, provider.model_name
    except Exception as e:
        issues.append(f"triage provider unavailable: {e}")

    if not TOKENS:
        # The point of this branch: a deploy that forgot ST_API_TOKENS is visible here
        # rather than quietly serving CPU-minutes to the internet.
        issues.append("auth disabled: ST_API_TOKENS is unset")

    pending = sum(1 for run in _runs.values() if run.status == "pending")
    return {
        "status": "degraded" if issues else "healthy",
        "provider": provider_name,
        "model": model,
        "auth": f"{len(TOKENS)} token(s)" if TOKENS else "disabled",
        # The limits this process actually loaded, not the ones the image ships --
        # appsrv's .env overrides image defaults, and Day 18 lost an evening to exactly
        # that with a stale SHA_MAX_ITERATIONS.
        "policy": {
            "risk_threshold": risk.THRESHOLD,
            "batch_size": triage.BATCH_SIZE,
            "max_body_bytes": MAX_BODY_BYTES,
            "max_runs_per_hour": MAX_RUNS_PER_HOUR,
            "window": RATE_WINDOW,
        },
        # A pending count that never drops means background runs are wedged against the
        # backend; nothing else in this service would show that.
        "runs": {"stored": len(_runs), "pending": pending, "capacity": MAX_RUNS},
        "issues": issues,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("ST_PORT", "7300")))
