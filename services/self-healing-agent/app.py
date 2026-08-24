"""FastAPI surface.

POST /diagnose runs the read-only loop and returns whatever it produced, including an
incomplete one -- deciding that an incomplete diagnosis is an error is not this module's
job. The write path does not run from here either: /diagnose records and posts a
proposal, and POST /slack/interactive turns a human's click into the only call that
reaches a write tool.

POST /alerts takes the human out of the loop's *front* end -- the trigger is Alertmanager
rather than curl, which is why this file has a background path and a /metrics scrape.
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

# From the first commit rather than bolted on at capstone.
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
    # None when the diagnosis proposed nothing, or proposed something that was not a write
    # tool call. Read this rather than re-deriving it from proposed_action.
    proposal_id: str | None = None


def _diagnose_and_propose(alert: dict):
    """One diagnosis, from alert to button. Shared by the two things that start one."""
    result = diagnose(alert, get_agent_provider())

    # Audited before it is posted, so a Slack outage costs the button and not the record.
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
        # 429, not 500: nothing is broken, the budget is spent. Also a status Alertmanager
        # will retry on.
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

    Catches everything: there is no response left to fail, and starlette would surface an
    escaping exception after the 202 has gone out. A guardrail refusal is a *decision*,
    already counted and audited, so logging and stopping is the correct end of that story.
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
    """Alertmanager's webhook.

    202 and not 200, because the diagnosis has been accepted rather than performed.
    Alertmanager gives up after seconds and re-POSTs; a diagnosis runs for minutes.

    A bare dict rather than a Pydantic model: a schema turns any future payload change into
    a 422, and a 422 makes Alertmanager retry a body that will never work.
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
    """Prometheus scrape target."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health_check():
    """Unauthenticated: a health check behind a token is one the container's own HEALTHCHECK
    cannot run.
    """
    issues: list[str] = []
    provider_name = model = "unknown"

    try:
        provider = get_agent_provider()
        provider_name, model = provider.name, provider.model_name
    except Exception as e:
        issues.append(f"agent provider unavailable: {e}")

    # Expected to fail outside a cluster, hence an issue and not a 500: the read-only tools
    # that need no cluster still work.
    try:
        k8s_client.get_apis()
        cluster = "reachable"
    except Exception as e:
        cluster = "unavailable"
        issues.append(f"kubernetes config unavailable: {e}")

    # A safety control, so an unwritable directory is a degraded service rather than a
    # surprise at the moment someone clicks Approve.
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
        # A dedup table empty while Alertmanager fires means the webhook is not arriving; one
        # that never empties means _prune stopped and every alert is being suppressed.
        "alerts": {
            "suppressed": len(alerts._seen),
            "dedup_ttl": alerts.DEDUP_TTL,
        },
        # What this process loaded, not what the image ships -- .env overrides image defaults,
        # and a stale SHA_MAX_ITERATIONS once cost an evening.
        "guards": {
            "namespaces": list(guardrails.NAMESPACES),
            "max_actions_per_hour": guardrails.MAX_ACTIONS_PER_HOUR,
            "breaker_threshold": guardrails.BREAKER_THRESHOLD,
            "max_llm_calls": guardrails.MAX_LLM_CALLS,
            "window": guardrails.WINDOW,
            "max_iterations": agent.MAX_ITERATIONS,
        },
        # A deploy that forgot SHA_API_TOKEN is visible here rather than quietly open.
        "auth": "required" if SHA_API_TOKEN else "disabled",
        "issues": issues,
    }


@app.post("/slack/interactive")
async def slack_interactive(request: Request):
    """The click. The HMAC is the authentication -- Slack cannot send a bearer token."""
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
