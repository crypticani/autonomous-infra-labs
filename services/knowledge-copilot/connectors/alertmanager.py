"""Alertmanager as a document source.

One function does I/O (`fetch_alerts`); everything else is pure, so rendering and
retention test against a recorded payload.

The corpus is markdown that changes when a human changes it. Alerts change on their own,
disappear when the condition clears, and repeat. Most of this module is about that.
"""

import logging
import os
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

from chunking import Document

load_dotenv()

logger = logging.getLogger(__name__)

ALERT_DOC_TYPE = "alert"

# Explicit rather than "whatever was stored", because the stored metadata also holds
# content_hash -- folding a hash into the input of the next hash makes it churn forever.
META_KEYS = ("severity", "instance", "service", "job")

# Stored for the same reason: a resolved alert rebuilt without them loses its summary and
# description, which is where the information lives.
ANNOTATION_KEYS = ("summary", "description")

# Long enough that a question at 09:00 can still find what woke someone at 03:00.
RETENTION_HOURS = int(os.getenv("ALERT_RETENTION_HOURS", "24"))

ALERTMANAGER_URL = os.getenv("ALERTMANAGER_URL", "http://localhost:9093")
FETCH_TIMEOUT = int(os.getenv("ALERTMANAGER_TIMEOUT", "10"))


class AlertmanagerError(RuntimeError):
    """The fetch did not succeed, so its result must not be reconciled against. Returning []
    would be indistinguishable from a quiet cluster, and merge() would conclude that every
    indexed alert resolved at once.
    """


def parse_ts(value: str) -> datetime:
    """Alertmanager emits RFC3339 with a trailing Z; fromisoformat takes it as of 3.11."""
    return datetime.fromisoformat(value)


def to_document(alert: dict, status: str, resolved_at: str | None = None) -> Document:
    """One alert, rendered as prose for the embedding model.

    Prose rather than JSON because the embedding model was trained on text. Every timestamp
    is absolute: "firing for 47 minutes" would change the text on every poll, which changes
    content_hash, which re-embeds the whole set every 60 seconds.
    """
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    alertname = labels.get("alertname", "UnnamedAlert")

    lines = [f"Alert: {alertname}", f"Status: {status}"]
    if severity := labels.get("severity"):
        lines.append(f"Severity: {severity}")

    scope = " | ".join(
        f"{key}: {labels[key]}"
        for key in ("service", "instance", "job")
        if key in labels
    )
    if scope:
        lines.append(scope)
    if summary := annotations.get("summary"):
        lines.append(f"Summary: {summary}")
    if description := annotations.get("description"):
        lines.append(f"Description: {description}")
    lines.append(f"Started: {alert['startsAt']}")
    if resolved_at:
        lines.append(f"Resolved: {resolved_at}")

    metadata = {
        "doc_type": ALERT_DOC_TYPE,
        "source": alertname,
        "fingerprint": alert["fingerprint"],
        "status": status,
        "started_at": alert["startsAt"],
        **{key: labels[key] for key in META_KEYS if key in labels},
        **{key: annotations[key] for key in ANNOTATION_KEYS if annotations.get(key)},
    }
    # Omitted rather than None: Chroma rejects None metadata values.
    if resolved_at:
        metadata["resolved_at"] = resolved_at

    return Document(
        slug=f"alert-{alert['fingerprint']}",
        text="\n".join(lines),
        metadata=metadata,
    )


def live_status(alert: dict, now: datetime) -> tuple[str, str | None]:
    """(status, resolved_at) for an alert Alertmanager still returns.

    endsAt on an active alert is in the future, so a past one means the alert has resolved.
    The `starts < ends` guard is for Alertmanager's zero value, which would otherwise read
    as "resolved two thousand years ago". A suppressed alert is silenced, still firing.
    """
    ends_at = alert.get("endsAt")
    if ends_at:
        ends = parse_ts(ends_at)
        if parse_ts(alert["startsAt"]) < ends <= now:
            return "resolved", ends_at
    if alert.get("status", {}).get("state") == "suppressed":
        return "silenced", None
    return "firing", None


def rebuild(meta: dict, resolved_at: str) -> Document:
    """Re-render a resolved alert from index metadata, because Alertmanager dropped it."""
    alert = {
        "fingerprint": meta["fingerprint"],
        "startsAt": meta["started_at"],
        "labels": {
            "alertname": meta["source"],
            **{key: meta[key] for key in META_KEYS if key in meta},
        },
        "annotations": {key: meta[key] for key in ANNOTATION_KEYS if key in meta},
    }
    return to_document(alert, status="resolved", resolved_at=resolved_at)


def merge(
    live: list[dict],
    indexed: dict[str, dict],
    now: datetime,
    retention_hours: int = RETENTION_HOURS,
) -> list[Document]:
    """The desired set: live alerts, plus recently-resolved ones still in the window."""
    documents = []
    seen = set()

    for alert in live:
        status, resolved_at = live_status(alert, now)
        documents.append(to_document(alert, status=status, resolved_at=resolved_at))
        seen.add(alert["fingerprint"])

    cutoff = now - timedelta(hours=retention_hours)
    for fingerprint, meta in indexed.items():
        if fingerprint in seen:
            continue
        # Keep the original timestamp if it was already resolved. Restamping it every
        # poll would make a resolved alert immortal -- always "just resolved", never
        # old enough to expire.
        #
        # Gated on `status`, not on the key being present, because Chroma's upsert
        # MERGES metadata rather than replacing it: once resolved_at is written it
        # survives every later write that omits it. So a flapping alert -- resolve,
        # re-fire, resolve again -- would read back the first resolution's timestamp,
        # and if that is more than retention_hours old it gets deleted immediately
        # instead of kept. status is rewritten on every poll; the stale key is not.
        already_resolved = meta.get("status") == "resolved" and meta.get("resolved_at")
        resolved_at = already_resolved or now.isoformat()
        if parse_ts(resolved_at) <= cutoff:
            # Falls out of `desired`, so plan_reconcile deletes it. Retention needs
            # no new code path -- only a correct desired set.
            continue
        documents.append(rebuild(meta, resolved_at=resolved_at))

    return documents


def fetch_alerts(
    url: str = ALERTMANAGER_URL, timeout: int = FETCH_TIMEOUT
) -> list[dict]:
    """Currently-known alerts from Alertmanager's v2 API."""
    try:
        response = requests.get(
            f"{url.rstrip('/')}/api/v2/alerts",
            params={"active": "true", "silenced": "true", "inhibited": "true"},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        raise AlertmanagerError(f"Alertmanager at {url} did not answer: {e}") from e
