"""FastAPI surface.

POST /triage answers 202 and a run id; GET /triage/{id} answers `pending` until done.
A run is one model call per ST_BATCH_SIZE findings, minutes each, so a synchronous
endpoint would time out on every real request and be retried.

Bearer auth, a body cap and a per-token rate limit are here from the first commit
because this is a public multi-tenant endpoint spending somebody else's CPU-minutes.
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
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

import metrics
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

# Plural: one token per onboarded repo, so revoking a leaked one is a list edit and the
# rate limit has something per-caller to count against.
TOKENS = {t.strip() for t in os.getenv("ST_API_TOKENS", "").split(",") if t.strip()}

# 16 MiB: a legitimate monorepo still fits, a body meant to exhaust memory does not.
MAX_BODY_BYTES = int(os.getenv("ST_MAX_BODY_BYTES", str(16 * 1024 * 1024)))

MAX_RUNS_PER_HOUR = int(os.getenv("ST_MAX_RUNS_PER_HOUR", "5"))
RATE_WINDOW = int(os.getenv("ST_RATE_WINDOW", "3600"))

# Run records are in-process, so the dict needs a ceiling. Oldest-first eviction, which
# is dict insertion order.
MAX_RUNS = int(os.getenv("ST_MAX_RUNS", "200"))

_runs: dict[str, "Run"] = {}
_starts: dict[str, list[float]] = {}


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
    """On Content-Length, not the parsed body -- by then the memory is already spent.

    Under-declaring gains nothing: uvicorn stops reading at the declared length. A chunked
    request declares none, so it is refused with 411; every real caller is a
    `curl --data-binary @file`, which always sends one.
    """
    if declared is None:
        return 411, "Content-Length is required on POST /triage"
    if not declared.isdigit() or int(declared) > MAX_BODY_BYTES:
        return 413, f"the scan envelope must be at most {MAX_BODY_BYTES} bytes"
    return None


def check_rate(caller: str) -> None:
    """One bucket per token, refused with 429."""
    now = time.monotonic()
    recent = [t for t in _starts.get(caller, []) if t > now - RATE_WINDOW]
    if len(recent) >= MAX_RUNS_PER_HOUR:
        metrics.REFUSALS.labels(reason="rate_limit").inc()
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
    """Middleware, not `Depends`, and that difference is the whole cap.

    FastAPI parses the body before it solves dependencies, so a `Depends` guard fires only
    after the megabytes it exists to refuse are already dicts. Returns a response rather
    than raising: an HTTPException here is outside FastAPI's handlers and 500s.
    """
    if request.method == "POST":
        error = body_size_error(request.headers.get("content-length"))
        if error:
            status, detail = error
            metrics.REFUSALS.labels(reason="body_size").inc()
            logger.warning(f"refused a body: {detail}")
            return JSONResponse({"detail": detail}, status_code=status)
    return await call_next(request)


class TriageRequest(BaseModel):
    """`scan.sh`'s envelope, plus one optional policy field."""

    repo: str
    commit: str = ""
    branch: str = ""
    scans: dict[str, Any] = Field(default_factory=dict)
    # Each repo sets its own bar; a public API and a cron script differ.
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
    # What a PR comment renders. The full judgment list is deliberately absent: nothing
    # reads it, and it would make every poll response megabytes of discarded JSON.
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

    Catches everything deliberately: an escaping exception would leave the run `pending`
    forever and the polling CI job spinning until its own timeout.

    `propose_fixes` gets the pre-dedup list and `triage_findings` the deduped one: nine
    securityContext rules share a (target, line) fingerprint, which is right for one
    judgment and wrong for a hunk that needs all nine keys.
    """
    run = _runs.get(run_id)
    if run is None:
        # Only if MAX_RUNS evicted the run between the 202 and the task starting.
        logger.warning(f"run {run_id} was evicted before its triage began")
        return

    label = metrics.repo_label(run.repo)
    started = time.monotonic()
    try:
        results = triage_findings(deduped)
        run.triaged = len(results)
        run.risk = assess(results, threshold=threshold)
        run.top = top_findings(results, deduped)
        run.fixes = propose_fixes(raw)
        run.status = "done"

        metrics.RUNS.labels(repo=label, outcome="done").inc()
        metrics.FINDINGS.labels(repo=label, stage="triaged").inc(run.triaged)
        metrics.VERDICTS.labels(repo=label, verdict=run.risk.verdict).inc()
        metrics.RISK_SCORE.labels(repo=label).observe(run.risk.score)
        # From `counts`, not by walking `results` again -- counting twice is how the two
        # numbers eventually disagree.
        for priority, count in run.risk.counts.items():
            metrics.PRIORITIES.labels(priority=priority).inc(count)

        logger.info(
            f"run {run_id} done: {run.triaged}/{len(deduped)} triaged, "
            f"score {run.risk.score} -> {run.risk.verdict}"
        )
    except TriageProviderError as e:
        run.status = "failed"
        run.error = f"{e.provider}: {e}"
        metrics.RUNS.labels(repo=label, outcome="failed").inc()
        logger.error(f"run {run_id} abandoned, {e.provider} failed: {e}")
    except Exception as e:
        run.status = "failed"
        run.error = str(e)
        metrics.RUNS.labels(repo=label, outcome="failed").inc()
        logger.exception(f"run {run_id} abandoned by an unexpected failure")
    finally:
        # Failures too: a run that dies on the ninth of ten batches already spent twenty
        # minutes, and a successes-only histogram calls that a quiet afternoon.
        metrics.RUN_DURATION.observe(time.monotonic() - started)


@app.post("/triage", status_code=202, response_model=TriageAccepted)
def start_triage(
    request: TriageRequest,
    background: BackgroundTasks,
    caller: str = Depends(require_token),
):
    """202 and a run id. The verdict arrives at GET /triage/{id}."""
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

    label = metrics.repo_label(request.repo)
    metrics.RUNS.labels(repo=label, outcome="accepted").inc()
    metrics.FINDINGS.labels(repo=label, stage="raw").inc(len(raw))
    metrics.FINDINGS.labels(repo=label, stage="deduped").inc(len(deduped))

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

    A process-local dict, so `--workers 1` is load-bearing and a restart mid-run strands a
    polling job on a 404 -- the loud version, and acceptable at this scale.

    ponytail: in-memory store. Upgrade path is a JSON file per run in a bind mount, the same
    shape as the agent's audit log.
    """
    run = _runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
    return run


@app.get("/metrics")
def metrics_endpoint():
    """Prometheus scrape target."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health_check():
    """Unauthenticated: a health check behind a token is one the container's own
    HEALTHCHECK cannot run.
    """
    issues: list[str] = []
    provider_name = model = "unknown"

    try:
        provider = get_triage_provider()
        provider_name, model = provider.name, provider.model_name
    except Exception as e:
        issues.append(f"triage provider unavailable: {e}")

    if not TOKENS:
        # A deploy that forgot ST_API_TOKENS is visible here rather than quietly
        # serving CPU-minutes to the internet.
        issues.append("auth disabled: ST_API_TOKENS is unset")

    pending = sum(1 for run in _runs.values() if run.status == "pending")
    return {
        "status": "degraded" if issues else "healthy",
        "provider": provider_name,
        "model": model,
        "auth": f"{len(TOKENS)} token(s)" if TOKENS else "disabled",
        # What this process loaded, not what the image ships -- .env overrides image
        # defaults, and a stale one has cost an evening before.
        "policy": {
            "risk_threshold": risk.THRESHOLD,
            "batch_size": triage.BATCH_SIZE,
            "max_body_bytes": MAX_BODY_BYTES,
            "max_runs_per_hour": MAX_RUNS_PER_HOUR,
            "window": RATE_WINDOW,
        },
        # A pending count that never drops means runs are wedged against the backend.
        "runs": {"stored": len(_runs), "pending": pending, "capacity": MAX_RUNS},
        "issues": issues,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("ST_PORT", "7300")))
