"""Day 13: the outbound half. The only module in the Slack path that touches network.

Raises rather than swallowing, the same discipline as connectors/alertmanager.py. A
failed post means someone is waiting on an answer that will never arrive, and that
belongs in the log as an error rather than in a bare `pass`.
"""

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
POST_TIMEOUT = int(os.getenv("SLACK_TIMEOUT", "10"))


class SlackError(RuntimeError):
    """chat.postMessage did not deliver."""


def post_message(
    channel: str, thread_ts: str, text: str, token: str | None = None
) -> None:
    token = SLACK_BOT_TOKEN if token is None else token
    try:
        response = requests.post(
            f"{SLACK_API}/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "thread_ts": thread_ts, "text": text},
            timeout=POST_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
    except requests.exceptions.RequestException as e:
        raise SlackError(f"chat.postMessage failed: {e}") from e

    # Slack signals application errors -- missing scope, unknown channel, archived
    # channel -- as ok:false inside a 200. Status alone is not success here.
    if not body.get("ok"):
        raise SlackError(f"chat.postMessage rejected: {body.get('error')}")

    logger.info(f"slack: posted to {channel} thread {thread_ts}")
