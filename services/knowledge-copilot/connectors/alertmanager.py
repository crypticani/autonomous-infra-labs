"""Day 12: Alertmanager as a document source.

One function does I/O (`fetch_alerts`). Everything else is pure, so rendering and
retention test against a recorded payload -- no network, no database.

The corpus is eleven markdown files that change when a human changes them. Alerts
change on their own, disappear when the condition clears, and repeat. Most of the
care in this module is about that difference.
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

# Carried from an alert's labels onto the chunk metadata, and -- for an alert that
# has resolved -- rebuilt from that metadata back into a document. The list is
# explicit rather than "whatever was stored" because the stored metadata also holds
# content_hash and indexed_at, and folding a hash into the input of the next hash
# makes it churn on every poll, forever.
META_KEYS = ("severity", "instance", "service", "job")

# Stored for the same reason, and learned the hard way: a resolved alert rebuilt
# without them loses its summary and description, which is where the actual
# information lives. "root filesystem is 79.3% full" is the sentence someone asking
# "what alerted overnight?" needs; alertname and two timestamps are a husk.
ANNOTATION_KEYS = ("summary", "description")

# How long a resolved alert stays in the index. Long enough that a question at 09:00
# can still find what woke someone at 03:00.
RETENTION_HOURS = int(os.getenv("ALERT_RETENTION_HOURS", "24"))

ALERTMANAGER_URL = os.getenv("ALERTMANAGER_URL", "http://localhost:9093")
FETCH_TIMEOUT = int(os.getenv("ALERTMANAGER_TIMEOUT", "10"))


class AlertmanagerError(RuntimeError):
    """The fetch did not succeed, so its result must not be reconciled against.

    Returning [] on failure would be indistinguishable from a quiet cluster, and
    merge() would conclude that every indexed alert resolved at once.
    """


def parse_ts(value: str) -> datetime:
    """Alertmanager emits RFC3339 with a trailing Z; fromisoformat takes it as of 3.11."""
    return datetime.fromisoformat(value)


def to_document(alert: dict, status: str, resolved_at: str | None = None) -> Document:
    """One alert, rendered as prose for the embedding model.

    Prose rather than JSON because the embedding model was trained on text --
    `{"labels":{"alertname":...}}` embeds poorly. BM25 tokenizes either form equally
    well, so the dense side is what decides the format.

    Every timestamp here is absolute. Rendering "firing for 47 minutes" would change
    the text on every poll, which changes content_hash, which makes plan_reconcile
    classify every alert as an update and re-embed the whole set every 60 seconds
    against a CPU-only Ollama. Duration is a display concern, not a document one.
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
    # Omitted rather than set to None: Chroma rejects None metadata values.
    if resolved_at:
        metadata["resolved_at"] = resolved_at

    return Document(
        slug=f"alert-{alert['fingerprint']}",
        text="\n".join(lines),
        metadata=metadata,
    )


def live_status(alert: dict, now: datetime) -> tuple[str, str | None]:
    """(status, resolved_at) for an alert Alertmanager still returns.

    endsAt on an active alert is startsAt + resolve_timeout, i.e. in the future, so a
    past endsAt means the alert has already resolved and is about to disappear. The
    `starts < ends` guard is for Alertmanager's zero value, "0001-01-01T00:00:00Z",
    which would otherwise read as "resolved two thousand years ago".

    A suppressed alert is silenced or inhibited -- still firing, someone muted the
    notification. Calling it resolved would be a lie about the state of the system.
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
    """Re-render a resolved alert from index metadata, because Alertmanager dropped it.

    Only the fields to_document needs are carried across -- see META_KEYS and
    ANNOTATION_KEYS. Passing the stored dict through wholesale would carry
    content_hash and indexed_at into the next hash computation, and the hash would
    never settle.
    """
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
    """The desired set: live alerts, plus recently-resolved ones still in the window.

    Alertmanager's /api/v2/alerts returns *active* alerts. A resolved one is retained
    only briefly (resolve_timeout, five minutes by default) and then disappears
    entirely -- there is no resolved-alert record to fetch. So resolution is detected
    by absence: an id in the index that a successful response did not mention.

    Which is why `live` MUST come from a successful fetch. An empty list here means
    every indexed alert resolved -- correct for a quiet cluster, catastrophic for a
    failed request. fetch_alerts raises rather than returning [] for exactly this.
    """
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
    """Currently-known alerts from Alertmanager's v2 API.

    Silenced and inhibited alerts are requested explicitly rather than relying on the
    API's defaults: they are still firing, and merge() renders them as such.
    """
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
