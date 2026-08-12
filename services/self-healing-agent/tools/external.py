"""The two tools that never touch the cluster: one reads Alertmanager, one reads
knowledge-copilot. Neither needs `apis`, so both ignore the first argument -- the same
uniform `fn(apis, **args)` dispatch shape as tools/k8s.py, without a second call
convention for the loop (Day 17) to special-case.
"""

import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from errors import RunbookError, UpstreamError

load_dotenv()

# Reused from the root .env, same as knowledge-copilot's own connector -- both services
# poll the one Alertmanager and should agree on where it is.
ALERTMANAGER_URL = os.getenv("ALERTMANAGER_URL", "http://localhost:9093")
ALERTMANAGER_TIMEOUT = int(os.getenv("ALERTMANAGER_TIMEOUT", "10"))

# Agent-only: the copilot's own address and this service's timeout for reaching it.
COPILOT_URL = os.getenv("SHA_KNOWLEDGE_COPILOT_URL", "http://localhost:7100")
COPILOT_TIMEOUT = int(os.getenv("SHA_KNOWLEDGE_COPILOT_TIMEOUT", "30"))

# The copilot's own bearer secret, reused rather than duplicated under an SHA_ key --
# it authenticates against /search-runbooks, the same door /ask-runbook uses.
KC_API_TOKEN = os.getenv("KC_API_TOKEN", "")


def get_recent_alerts(
    apis, *, service: str | None = None, since_minutes: int = 60
) -> dict:
    """The same source knowledge-copilot polls, so both services agree on what's firing."""
    try:
        response = requests.get(
            f"{ALERTMANAGER_URL.rstrip('/')}/api/v2/alerts",
            params={"active": "true", "silenced": "true", "inhibited": "true"},
            timeout=ALERTMANAGER_TIMEOUT,
        )
        response.raise_for_status()
        alerts = response.json()
    except requests.exceptions.RequestException as e:
        # No dedicated AlertmanagerError subclass: errors.py's taxonomy is declared in
        # full on day one, and this upstream isn't in it. UpstreamError already carries
        # `provider` for exactly this case.
        raise UpstreamError(
            f"Alertmanager at {ALERTMANAGER_URL} did not answer: {e}",
            502,
            provider="alertmanager",
        ) from e

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    matches = []
    for alert in alerts:
        labels = alert.get("labels", {})
        if service and labels.get("service") != service:
            continue
        if datetime.fromisoformat(alert["startsAt"]) < cutoff:
            continue
        matches.append(
            {
                "alertname": labels.get("alertname", "UnnamedAlert"),
                "severity": labels.get("severity"),
                "service": labels.get("service"),
                "summary": alert.get("annotations", {}).get("summary"),
                "started_at": alert["startsAt"],
                "status": alert.get("status", {}).get("state", "active"),
            }
        )
    return {"since_minutes": since_minutes, "service": service, "alerts": matches}


def search_runbooks(apis, *, question: str, k: int = 4) -> dict:
    """Retrieval only, over HTTP -- never a library import.

    Chroma's PersistentClient is not safe for multi-process access, and knowledge-copilot
    writes to that index every 60 seconds during alert sync; a second process holding
    its own HNSW index would read stale vectors. This service's Docker build context
    also cannot `COPY` a sibling service's modules.
    """
    headers = {"Authorization": f"Bearer {KC_API_TOKEN}"} if KC_API_TOKEN else {}
    try:
        response = requests.post(
            f"{COPILOT_URL.rstrip('/')}/search-runbooks",
            json={"question": question, "k": k},
            headers=headers,
            timeout=COPILOT_TIMEOUT,
        )
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RunbookError(
            f"knowledge-copilot returned {e.response.status_code}: {e}",
            e.response.status_code,
        ) from e
    except requests.exceptions.RequestException as e:
        raise RunbookError(
            f"knowledge-copilot at {COPILOT_URL} did not answer: {e}", 502
        ) from e
    return response.json()
