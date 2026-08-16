"""Alertmanager's webhook body, turned into the alerts diagnose() already takes -- Day 20.

This module decides *which* alerts are worth a diagnosis. It never runs one, and it never
calls a model: everything here is arithmetic on a dict, which is what makes the expensive
decision testable without a key.

Day 16 built the outbound direction -- tools/external.get_recent_alerts polls Alertmanager
for what else is firing. This is the inbound one, and it closes the loop: until now every
diagnosis started with a human running curl.

The alert is passed through unchanged rather than reshaped. get_recent_alerts flattens,
because there it is context -- "what else is going on" -- and the summary is the point. Here
the alert *is* the problem, and every field dropped is a field the model cannot reason
about. `namespace` and `pod` most of all: those are the arguments get_pod_logs needs, and a
tidier dict that loses them buys nothing and costs the diagnosis.
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
    """Alertmanager's own fingerprint when it sent one, the labels otherwise.

    The fallback is not a nicety. Falling back to something unique-per-delivery -- a
    timestamp, id() -- would mean no deduplication at all, silently, which is worse than
    not deduplicating on purpose. Labels are what Alertmanager fingerprints anyway.
    """
    given = alert.get("fingerprint")
    if given:
        return str(given)
    labels = alert.get("labels") or {}
    return repr(sorted(labels.items()))


def accept(payload: dict, now: float | None = None) -> Intake:
    """Split one webhook body into the alerts worth diagnosing, and the reasons the rest
    were not.

    Defensive about shape on purpose: this is an unparsed POST from another process. A
    KeyError here is a 500, and a 500 makes Alertmanager retry a body that will never
    work -- forever, every group_interval.
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
