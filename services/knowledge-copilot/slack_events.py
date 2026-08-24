"""The inbound half of the Slack interface.

Every function here is pure. Signature verification, mention cleaning and retry dedupe
are the parts that break in production and the parts a fabricated request can exercise
offline, so none of them touch the network.
"""

import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_ENABLED = os.getenv("SLACK_ENABLED", "true").lower() == "true"

# Slack rejects its own requests older than this and so do we: the HMAC never expires,
# so without an age check a captured request stays replayable forever.
MAX_SIGNATURE_AGE = 300

# The shortest question worth embedding. HTTP callers get min_length=10; a mention needs
# its own check, because a 422 has nowhere to go inside a Slack thread.
MIN_QUESTION_LENGTH = 10

# Permissive: ids start U or W, and the legacy form carries a label.
MENTION_RE = re.compile(r"<@[^>]+>")

# The only three Slack escapes. Ampersand is undone last, or "&amp;lt;" decodes twice.
SLACK_ESCAPES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))

# event_id -> first seen. Only useful for about a minute, so it is swept, not grown.
DEDUPE_TTL = 300
_seen_events: dict[str, float] = {}


@dataclass(frozen=True)
class Mention:
    channel: str
    thread_ts: str
    question: str


def slack_active() -> bool:
    """The flag *and* both secrets. The offline suite and a local `python app.py` have no
    credentials, so missing secrets disable the route rather than failing startup.
    """
    return bool(SLACK_ENABLED and SLACK_SIGNING_SECRET and SLACK_BOT_TOKEN)


def verify_signature(
    body: bytes,
    timestamp: str,
    signature: str,
    now: float,
    secret: str | None = None,
) -> bool:
    """Slack's v0 request signature.

    `body` must be the raw bytes off the wire -- re-serialising changes whitespace and key
    order. The secret is read from the module global rather than defaulted, because a
    default argument binds at import and cannot be patched.
    """
    secret = SLACK_SIGNING_SECRET if secret is None else secret
    if not (timestamp and signature and secret):
        return False
    try:
        age = abs(now - float(timestamp))
    except ValueError:
        return False
    if age > MAX_SIGNATURE_AGE:
        return False

    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)


def clean_text(text: str) -> str:
    """Strip the @mention tokens and undo Slack's escaping. Only the three entities Slack
    produces: html.unescape would also decode &copy;, which a runbook question could
    legitimately contain.
    """
    cleaned = MENTION_RE.sub("", text)
    for entity, char in SLACK_ESCAPES:
        cleaned = cleaned.replace(entity, char)
    # Collapses the double space left where the mention was.
    return " ".join(cleaned.split())


def parse_mention(event: dict) -> Mention | None:
    """The question, channel and thread from an app_mention event. None means "not ours to
    answer". Threading uses thread_ts inside a thread and ts when starting one.
    """
    if event.get("bot_id") or event.get("subtype"):
        return None
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    if not (channel and thread_ts):
        return None
    return Mention(
        channel=channel,
        thread_ts=thread_ts,
        question=clean_text(event.get("text", "")),
    )


def is_duplicate(event_id: str, now: float) -> bool:
    """True if this event has already been accepted. Slack resends on a non-200 or a response
    slower than three seconds, and at ~195 seconds an undeduped retry triples contention on
    a CPU that can serve one.
    """
    for stale in [eid for eid, seen in _seen_events.items() if now - seen > DEDUPE_TTL]:
        del _seen_events[stale]
    if event_id in _seen_events:
        return True
    _seen_events[event_id] = now
    return False
