"""FastAPI surface -- one endpoint today. POST /diagnose runs the read-only loop and
returns whatever it produced, including an incomplete one; it is not this module's job
to decide that an incomplete diagnosis is an error.
"""

import hmac
import logging
import os
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from agent import diagnose
from errors import UpstreamError
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
    description="Diagnoses Kubernetes alerts with read-only tools; proposes, never acts",
    version="1.0.0",
)


class DiagnoseRequest(BaseModel):
    alert: dict[str, Any]


class DiagnoseResponse(BaseModel):
    summary: str | None
    evidence: list[str]
    proposed_action: dict | None
    confidence: float | None
    incomplete: bool


@app.post(
    "/diagnose",
    response_model=DiagnoseResponse,
    dependencies=[Depends(require_token)],
)
def diagnose_alert(request: DiagnoseRequest):
    logger.info(f"/diagnose alert={request.alert!r}")
    try:
        result = diagnose(request.alert, get_agent_provider())
    except UpstreamError as e:
        logger.warning(f"{e}")
        raise HTTPException(status_code=e.status, detail=str(e))
    return DiagnoseResponse(
        summary=result.summary,
        evidence=list(result.evidence),
        proposed_action=result.proposed_action,
        confidence=result.confidence,
        incomplete=result.incomplete,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SHA_PORT", "7200")))
