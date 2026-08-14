"""The Slack surface for approvals -- Day 18.

verify_signature is a deliberate copy of knowledge-copilot's slack_events.py rather
than an import: these are two services in two containers, and the alternative to
copying thirty lines is a shared package that couples their deploys. The copy is the
cheaper of the two.

What is genuinely new is the payload shape. /slack/events posts JSON; interactive
components post form-encoded, with the whole interaction JSON-encoded under a single
`payload` key. The HMAC is still computed over the raw bytes either way.
"""

import hashlib
import hmac
import json
import logging
import os
import urllib.parse
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_ENABLED = os.getenv("SLACK_ENABLED", "true").lower() == "true"
POST_TIMEOUT = int(os.getenv("SLACK_TIMEOUT", "10"))

# Slack rejects its own requests older than this and so do we: the HMAC never expires
# on its own, so without an age check a captured request stays replayable forever.
MAX_SIGNATURE_AGE = 300


class SlackError(RuntimeError):
    """Slack never answered, or answered by refusing the call."""


@dataclass(frozen=True)
class Interaction:
    proposal_id: str
    decision: str  # "approve" | "reject"
    user: str
    response_url: str


def slack_active() -> bool:
    """The flag and both secrets, same rule as the copilot: the offline test suite and
    a local `python app.py` have no credentials, so missing secrets disable the post
    rather than failing startup."""
    return bool(SLACK_ENABLED and SLACK_SIGNING_SECRET and SLACK_BOT_TOKEN)


def verify_signature(
    body: bytes,
    timestamp: str,
    signature: str,
    now: float,
    secret: str | None = None,
) -> bool:
    """Slack's v0 request signature, over the raw bytes off the wire.

    Parsing the form and re-encoding it changes escaping and key order, and the HMAC
    then never matches -- which is why app.py reads request.body() before it reads
    anything else.

    The secret is read from the module global when not passed, rather than defaulting
    to it: a default argument binds once at import and could not be patched in tests.
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


def parse_interaction(body: bytes) -> Interaction | None:
    """`payload=<url-encoded JSON>` -- the one thing that differs from Day 13.

    None means "not an approval button", which covers Slack's other interactive
    payloads as well as anything malformed. The caller answers 200 either way: a
    non-200 makes Slack retry, and retrying a button click is the last thing wanted on
    a path that ends in a cluster write.
    """
    try:
        fields = urllib.parse.parse_qs(body.decode("utf-8"))
        payload = json.loads(fields["payload"][0])
        action = payload["actions"][0]
    except (UnicodeDecodeError, KeyError, IndexError, ValueError) as e:
        logger.warning(f"unparseable interaction payload: {e}")
        return None

    decision = action.get("action_id", "")
    if decision not in ("approve", "reject"):
        return None

    user = payload.get("user", {})
    return Interaction(
        proposal_id=action.get("value", ""),
        decision=decision,
        user=user.get("username") or user.get("id") or "unknown",
        response_url=payload.get("response_url", ""),
    )


def blocks_for(proposal) -> list[dict]:
    """The action verbatim, then the buttons.

    The arguments are rendered as JSON rather than prose because the human is being
    asked to approve exactly what will be executed, and a summary of it is not that.
    The confirm dialog on Approve is there because the button sits in a channel where
    a mis-tap is one pixel away from deleting a pod.
    """
    confidence = (
        "unknown" if proposal.confidence is None else f"{proposal.confidence:.2f}"
    )
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Action proposed"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{proposal.summary}*"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Tool*\n`{proposal.tool}`"},
                {"type": "mrkdwn", "text": f"*Confidence*\n{confidence}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"```{json.dumps(proposal.args, indent=2)}```",
            },
        },
        {
            "type": "actions",
            "block_id": proposal.id,
            "elements": [
                {
                    "type": "button",
                    "action_id": "approve",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "value": proposal.id,
                    "confirm": {
                        "title": {
                            "type": "plain_text",
                            "text": "Run this against the cluster?",
                        },
                        "text": {
                            "type": "mrkdwn",
                            "text": f"`{proposal.tool}` with the arguments above.",
                        },
                        "confirm": {"type": "plain_text", "text": "Run it"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "action_id": "reject",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": proposal.id,
                },
            ],
        },
    ]


def post_blocks(
    channel: str,
    blocks: list[dict],
    text: str = "Action proposed",
    token: str | None = None,
) -> None:
    """Raises rather than swallowing, the same discipline as the copilot's
    slack_client: a proposal nobody can see is a proposal nobody can approve."""
    token = SLACK_BOT_TOKEN if token is None else token
    try:
        response = requests.post(
            f"{SLACK_API}/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "text": text, "blocks": blocks},
            timeout=POST_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
    except requests.exceptions.RequestException as e:
        raise SlackError(f"chat.postMessage failed: {e}") from e

    # Slack signals application errors -- missing scope, unknown channel -- as ok:false
    # inside a 200. Status alone is not success here.
    if not body.get("ok"):
        raise SlackError(f"chat.postMessage rejected: {body.get('error')}")

    logger.info(f"slack: posted a proposal to {channel}")


def replace_message(response_url: str, text: str) -> None:
    """Replaces the whole message, buttons included, once a decision is recorded.

    Best-effort by design, and the one place in this module that swallows: by the time
    this runs the decision is made, audited and executed, so raising would turn a
    cosmetic failure into a 500 on a request whose real work succeeded. Stale buttons
    are the lesser problem, and decide() refuses them anyway.
    """
    if not response_url:
        return
    try:
        requests.post(
            response_url,
            json={"replace_original": True, "text": text},
            timeout=POST_TIMEOUT,
        ).raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.warning(f"could not replace the message in Slack: {e}")
