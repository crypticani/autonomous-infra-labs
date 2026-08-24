"""Alertmanager's webhook body, turned into the alerts diagnose() already takes.

Decides *which* alerts are worth a diagnosis. Never runs one and never calls a model --
everything here is arithmetic on a dict, which is what makes the expensive decision
testable without a key.

The alert is passed through unchanged rather than reshaped: here the alert *is* the
problem, and `namespace` and `pod` are the arguments get_pod_logs needs.
"""

import logging
import os
import time
from dataclasses import dataclass

import metrics

logger = logging.getLogger(__name__)

# An hour, matching SHA_GUARD_WINDOW and the proposal TTL, and not by accident: all three
# answer the same question -- how long ago does something have to have happened before it
# stops counting as the same incident.
DEDUP_TTL = int(os.getenv("SHA_ALERT_DEDUP_TTL", "3600"))

# fingerprint -> when it was last accepted. Process state, like approvals._proposals, and
# coherent for the same reason: one uvicorn worker. Two workers would each keep half the
# memory and deduplicate nothing, which is the failure this exists to prevent.
_seen: dict[str, float] = {}


@dataclass(frozen=True)
class Intake:
    """What one webhook body amounted to. The two counts are not diagnostics: they are
    what the endpoint returns and what the metrics label, so a silent drop is impossible.
    """

    accepted: tuple[dict, ...]
    resolved: int
    duplicate: int


def _fingerprint(alert: dict) -> str:
    """Alertmanager's own fingerprint when it sent one, the labels otherwise."""
    given = alert.get("fingerprint")
    if given:
        return str(given)
    labels = alert.get("labels") or {}
    return repr(sorted(labels.items()))


def accept(payload: dict, now: float | None = None) -> Intake:
    """Split one webhook body into the alerts worth diagnosing, and the reasons the rest
    were not.
    """
    now = time.time() if now is None else now
    _prune(now)

    accepted, resolved, duplicate = [], 0, 0
    for alert in payload.get("alerts") or []:
        if not isinstance(alert, dict):
            logger.warning(f"ignoring a webhook entry that is not an alert: {alert!r}")
            continue

        # Alertmanager sends the same webhook when an alert clears. There is nothing to
        # diagnose about a problem that has already stopped.
        if alert.get("status") == "resolved":
            resolved += 1
            metrics.ALERTS_RECEIVED.labels(outcome="resolved").inc()
            continue

        fingerprint = _fingerprint(alert)
        if fingerprint in _seen:
            duplicate += 1
            metrics.ALERTS_RECEIVED.labels(outcome="duplicate").inc()
            continue

        _seen[fingerprint] = now
        accepted.append(alert)
        metrics.ALERTS_RECEIVED.labels(outcome="accepted").inc()

    if resolved or duplicate:
        logger.info(
            f"webhook: {len(accepted)} to diagnose, {resolved} resolved, "
            f"{duplicate} already seen"
        )
    return Intake(accepted=tuple(accepted), resolved=resolved, duplicate=duplicate)


def _prune(now: float) -> None:
    """Suppression with an expiry, not amnesia. An alert still firing an hour later has
    outlived the proposal made for it, and deserves another look -- and this is also what
    stops _seen growing for the life of the process.
    """
    for fingerprint, seen_at in list(_seen.items()):
        if now - seen_at >= DEDUP_TTL:
            del _seen[fingerprint]
