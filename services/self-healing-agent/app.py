"""FastAPI surface. POST /diagnose runs the read-only loop and returns whatever it
produced, including an incomplete one; it is not this module's job to decide that an
incomplete diagnosis is an error.

Day 18 adds the write path, and it does not run from here either: /diagnose records
and posts a proposal, and POST /slack/interactive turns a human's click into the only
call that reaches a write tool.

Day 20 adds POST /alerts, and with it the last human out of the loop's *front* end:
until now every diagnosis began with someone running curl. The human is still in the
loop where it counts -- nothing writes to a cluster without a click -- but the trigger is
now Alertmanager, which is why this file grew a background path and a /metrics scrape.
"""

import hmac
import logging
import os
import time
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

import agent
import alerts
import approvals
import audit
import guardrails
import k8s_client
import slack
from agent import diagnose
from errors import GuardrailViolation, UpstreamError
from provider import get_agent_provider

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logger = logging.getLogger(__name__)

# From the first commit, not bolted on later -- copied from knowledge-copilot's
# require_token. Bolting auth on at capstone is how Week 2 ended up doing it on Day 14.
SHA_API_TOKEN = os.getenv("SHA_API_TOKEN", "")


def require_token(request: Request) -> None:
    if not SHA_API_TOKEN:
        return
    scheme, _, presented = request.headers.get("Authorization", "").partition(" ")
    if scheme != "Bearer" or not hmac.compare_digest(presented, SHA_API_TOKEN):
        raise HTTPException(
            status_code=401,
            detail="a valid bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


app = FastAPI(
    title="Self-Healing Agent",
    description="Diagnoses Kubernetes alerts with read-only tools; acts only on a "
    "human's approval",
    version="1.1.0",
)


class DiagnoseRequest(BaseModel):
    alert: dict[str, Any]


class DiagnoseResponse(BaseModel):
    summary: str | None
    evidence: list[str]
    proposed_action: dict | None
    confidence: float | None
    incomplete: bool
    # None when the diagnosis proposed nothing, or proposed something that was not a
    # write tool call. A caller that wants to know whether a human was asked reads
    # this rather than re-deriving it from proposed_action.
    proposal_id: str | None = None


def _diagnose_and_propose(alert: dict):
    """One diagnosis, from alert to button. Shared by the two things that start one.

    Extracted on Day 20 rather than duplicated into the webhook, because the two callers
    differ only in what they do with the *outcome* -- /diagnose returns it, /alerts has
    no one left to return it to. Duplicating meant two places to remember that a Slack
    outage must not discard a diagnosis, and the second copy is the one that forgets.
    """
    result = diagnose(alert, get_agent_provider())

    # The proposal is audited before it is posted, so a Slack outage costs the button
    # and not the record. Failing here because a chat API was down would discard work
    # that succeeded and is already on disk.
    proposal = None
    try:
        proposal = approvals.propose(result, alert)
    except slack.SlackError as e:
        logger.error(f"proposal recorded but not posted to slack: {e}")
    return result, proposal


@app.post(
    "/diagnose",
    response_model=DiagnoseResponse,
    dependencies=[Depends(require_token)],
)
def diagnose_alert(request: DiagnoseRequest):
    logger.info(f"/diagnose alert={request.alert!r}")
    try:
        result, proposal = _diagnose_and_propose(request.alert)
    except GuardrailViolation as e:
        # 429, not 500: nothing is broken. The model-call budget this deploy was given is
        # spent, and the honest answer to "diagnose this now" is "not until the window
        # rolls" -- which is also a status code Alertmanager's webhook will retry on.
        logger.warning(f"guardrail {e.guard!r} refused this diagnosis: {e}")
        raise HTTPException(status_code=429, detail=str(e))
    except UpstreamError as e:
        logger.warning(f"{e}")
        raise HTTPException(status_code=e.status, detail=str(e))

    return DiagnoseResponse(
        summary=result.summary,
        evidence=list(result.evidence),
        proposed_action=result.proposed_action,
        confidence=result.confidence,
        incomplete=result.incomplete,
        proposal_id=proposal.id if proposal else None,
    )


def _diagnose_in_background(alert: dict) -> None:
    """The same work, with nobody to report to.

    Catches everything, deliberately. There is no response left to fail, so an escaping
    exception buys a traceback nobody reads -- and worse, starlette would surface it
    after the 202 has already gone out, which is the worst of both: Alertmanager thinks
    it succeeded and the log says otherwise.

    A guardrail refusal in particular is a *decision*, already counted and audited by the
    time it arrives here. Logging it at warning and stopping is the correct end of that
    story, not an error.
    """
    try:
        _diagnose_and_propose(alert)
    except GuardrailViolation as e:
        logger.warning(f"guardrail {e.guard!r} refused this alert: {e}")
    except UpstreamError as e:
        logger.error(f"diagnosis abandoned, {e.provider} failed: {e}")
    except Exception:
        logger.exception("diagnosis abandoned by an unexpected failure")


@app.post("/alerts", status_code=202, dependencies=[Depends(require_token)])
def receive_alerts(payload: dict, background: BackgroundTasks):
    """Alertmanager's webhook. Day 20, and the end of curl as the way in.

    202 and not 200, because the diagnosis has been accepted rather than performed.
    Alertmanager's webhook client gives up after seconds and re-POSTs the group; a
    diagnosis is six to ten model calls and runs for minutes. Answering synchronously
    would guarantee a timeout on every alert *and* a duplicate diagnosis behind it --
    two calls into a free tier of about twenty a day.

    The body is taken as a bare dict rather than a Pydantic model on purpose. A schema
    here turns anything Alertmanager's future changes to the payload into a 422, and a
    422 makes it retry a body that will never work; alerts.accept is written to be
    defensive about shape instead, which is where the knowledge belongs.
    """
    intake = alerts.accept(payload)
    logger.info(
        f"/alerts accepted={len(intake.accepted)} resolved={intake.resolved} "
        f"duplicate={intake.duplicate}"
    )
    for alert in intake.accepted:
        background.add_task(_diagnose_in_background, alert)

    return {
        "accepted": len(intake.accepted),
        "resolved": intake.resolved,
        "duplicate": intake.duplicate,
    }


@app.get("/metrics")
def metrics_endpoint():
    """Prometheus scrape target.

    Unauthenticated, exactly like the copilot's: Prometheus reaches this over loopback on
    appsrv, and a bearer token in a scrape config is a secret in a third place buying
    nothing.

    Nothing is computed here. Every metric in this service is a counter or a histogram
    incremented at the moment the thing happened, so there is no live state to read at
    scrape time and no way for a scrape to fail.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health_check():
    """Unauthenticated, like the copilot's: a health check behind a bearer token is a
    health check the container's own HEALTHCHECK cannot run.

    Every dependency is *probed*, not just named. Constructing a provider does no I/O,
    so reporting the configured model without touching anything would say "healthy"
    for a deploy whose API key is wrong -- which reaches a caller as a 502 mid-
    diagnosis instead of a degraded check here.
    """
    issues: list[str] = []
    provider_name = model = "unknown"

    try:
        provider = get_agent_provider()
        provider_name, model = provider.name, provider.model_name
    except Exception as e:
        issues.append(f"agent provider unavailable: {e}")

    # Expected to fail everywhere except in-cluster, which is why it is an issue and
    # not a 500: the read-only tools that need no cluster still work without one.
    try:
        k8s_client.get_apis()
        cluster = "reachable"
    except Exception as e:
        cluster = "unavailable"
        issues.append(f"kubernetes config unavailable: {e}")

    # The audit log is a safety control, so a directory that is not writable is a
    # degraded service and not a surprise at the moment someone clicks Approve.
    audit_dir = os.path.dirname(os.path.abspath(audit.AUDIT_PATH))
    audit_writable = os.access(audit_dir, os.W_OK)
    if not audit_writable:
        issues.append(f"audit path {audit.AUDIT_PATH!r} is not writable")

    if not approvals.slack_enabled():
        issues.append("slack approvals inactive: no write tool can be approved")

    return {
        "status": "degraded" if issues else "healthy",
        "provider": provider_name,
        "model": model,
        "cluster": cluster,
        "slack": "active" if approvals.slack_enabled() else "disabled",
        "audit_path": audit.AUDIT_PATH,
        "pending_proposals": len(approvals._proposals),
        "proposal_ttl": approvals.PROPOSAL_TTL,
        # Day 20. A dedup table that is empty while Alertmanager is firing means the
        # webhook is not arriving; one that never empties means _prune stopped running
        # and every future alert is being suppressed. Neither is visible anywhere else.
        "alerts": {
            "suppressed": len(alerts._seen),
            "dedup_ttl": alerts.DEDUP_TTL,
        },
        # The limits this process actually loaded, not the ones the image ships. appsrv's
        # .env overrides image defaults, and on Day 18 that cost an evening to a
        # SHA_MAX_ITERATIONS still set to 6 -- invisible until it truncated a diagnosis.
        "guards": {
            "namespaces": list(guardrails.NAMESPACES),
            "max_actions_per_hour": guardrails.MAX_ACTIONS_PER_HOUR,
            "breaker_threshold": guardrails.BREAKER_THRESHOLD,
            "max_llm_calls": guardrails.MAX_LLM_CALLS,
            "window": guardrails.WINDOW,
            "max_iterations": agent.MAX_ITERATIONS,
        },
        # The point of the unset branch: a deploy that forgot SHA_API_TOKEN is visible
        # here, rather than being quietly open.
        "auth": "required" if SHA_API_TOKEN else "disabled",
        "issues": issues,
    }


@app.post("/slack/interactive")
async def slack_interactive(request: Request):
    """The click. No Depends(require_token) here: the HMAC is the authentication,
    exactly as on the copilot's /slack/events. Slack cannot send a bearer token, and
    adding one would only put a shared secret in a URL somewhere.

    `await request.body()` comes first and nothing re-serialises it, because the
    signature is over the raw bytes -- parsing the form and re-encoding it changes
    escaping and key order, and the HMAC then never matches.

    Once the signature checks out the answer is always 200. A non-200 makes Slack
    retry, and a retried click on this route is a second attempt at a cluster write;
    approvals.decide() would refuse it, but the better place not to have the problem
    is here.
    """
    raw = await request.body()
    if not slack.verify_signature(
        raw,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        request.headers.get("X-Slack-Signature", ""),
        time.time(),
    ):
        logger.warning("rejected an interaction with a bad or missing signature")
        raise HTTPException(status_code=401, detail="bad signature")

    interaction = slack.parse_interaction(raw)
    if interaction is None:
        return {"ok": True}

    logger.info(
        f"/slack/interactive {interaction.decision} "
        f"{interaction.proposal_id} by {interaction.user}"
    )
    outcome = approvals.decide(
        interaction.proposal_id, interaction.decision, interaction.user
    )
    slack.replace_message(interaction.response_url, outcome.message)
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SHA_PORT", "7200")))
