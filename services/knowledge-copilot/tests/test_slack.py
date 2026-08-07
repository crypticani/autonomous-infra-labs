import asyncio
import hashlib
import hmac
import json
import time

import pytest
import requests
from fastapi.testclient import TestClient

import app as app_module
from app import ACK_TEXT, FAILED, INDEX_EMPTY, MODEL_DOWN, TOO_SHORT, app
from llm import UpstreamError
from retrieval import EmptyIndexError
from slack_client import SlackError, post_message
from slack_events import (
    MAX_SIGNATURE_AGE,
    Mention,
    clean_text,
    is_duplicate,
    parse_mention,
    verify_signature,
)

client = TestClient(app)

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
NOW = 1755000000.0
TS = "1755000000"


def sign(body: bytes, timestamp: str, secret: str = SECRET) -> str:
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


# --- signature ---------------------------------------------------------------


def test_a_correctly_signed_request_verifies():
    body = b'{"type":"event_callback"}'
    assert verify_signature(body, TS, sign(body, TS), now=NOW, secret=SECRET) is True


def test_the_wrong_secret_fails():
    body = b'{"type":"event_callback"}'
    forged = sign(body, TS, secret="not-the-signing-secret")
    assert verify_signature(body, TS, forged, now=NOW, secret=SECRET) is False


def test_a_tampered_body_fails():
    signature = sign(b'{"text":"harmless"}', TS)
    assert (
        verify_signature(b'{"text":"replaced"}', TS, signature, now=NOW, secret=SECRET)
        is False
    )


def test_a_stale_timestamp_fails_even_with_a_valid_signature():
    body = b"{}"
    signature = sign(body, TS)
    # The replay guard. The HMAC itself stays valid forever, so a captured request
    # would be replayable without this.
    late = NOW + MAX_SIGNATURE_AGE + 1
    assert verify_signature(body, TS, signature, now=late, secret=SECRET) is False


def test_a_future_timestamp_fails():
    body = b"{}"
    signature = sign(body, TS)
    early = NOW - MAX_SIGNATURE_AGE - 1
    assert verify_signature(body, TS, signature, now=early, secret=SECRET) is False


def test_missing_headers_fail():
    assert verify_signature(b"{}", "", "", now=NOW, secret=SECRET) is False


def test_a_non_numeric_timestamp_fails():
    assert (
        verify_signature(b"{}", "yesterday", "v0=abc", now=NOW, secret=SECRET) is False
    )


def test_no_configured_secret_fails_closed():
    body = b"{}"
    assert verify_signature(body, TS, sign(body, TS), now=NOW, secret="") is False


# --- mention text ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "<@U08ABC123> why is appsrv disk filling up?",
            "why is appsrv disk filling up?",
        ),
        # Slack escapes exactly &, < and >. Without unescaping, this gets embedded
        # with the entities still in it.
        (
            "<@U08ABC123> alert if usage &gt; 90 &amp;&amp; page me",
            "alert if usage > 90 && page me",
        ),
        ("hey <@U08ABC123> what alerted overnight?", "hey what alerted overnight?"),
        ("<@U08ABC123>", ""),
    ],
)
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


# --- mention parsing ---------------------------------------------------------


def test_a_mention_starting_a_thread_replies_under_the_parent():
    event = {
        "type": "app_mention",
        "channel": "C1",
        "ts": "1755000000.000100",
        "text": "<@U08ABC123> why is appsrv disk filling up?",
    }
    assert parse_mention(event) == Mention(
        channel="C1",
        thread_ts="1755000000.000100",
        question="why is appsrv disk filling up?",
    )


def test_a_mention_inside_a_thread_stays_in_that_thread():
    event = {
        "type": "app_mention",
        "channel": "C1",
        "ts": "1755000900.000200",
        "thread_ts": "1755000000.000100",
        "text": "<@U08ABC123> and the command to clear it?",
    }
    # thread_ts wins over ts, or the reply would branch a new thread off the follow-up.
    assert parse_mention(event).thread_ts == "1755000000.000100"


def test_a_bot_message_is_ignored():
    event = {"channel": "C1", "ts": "1", "bot_id": "B123", "text": "<@U1> hello"}
    assert parse_mention(event) is None


def test_an_event_without_a_channel_is_ignored():
    assert parse_mention({"ts": "1", "text": "<@U1> hello"}) is None


# --- retry dedupe ------------------------------------------------------------


def test_the_first_sighting_is_not_a_duplicate():
    assert is_duplicate("Ev0001", now=NOW) is False


def test_a_retried_event_is_a_duplicate():
    is_duplicate("Ev0001", now=NOW)
    # Slack retries up to three times on a slow ack. Each retry that got through would
    # be another 195-second job on a CPU that can just about serve one.
    assert is_duplicate("Ev0001", now=NOW + 1) is True
    assert is_duplicate("Ev0001", now=NOW + 2) is True


def test_different_events_are_not_duplicates():
    is_duplicate("Ev0001", now=NOW)
    assert is_duplicate("Ev0002", now=NOW) is False


# --- outbound ----------------------------------------------------------------


class FakePost:
    """Stands in for requests.post and for the response it returns."""

    def __init__(self, payload, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


def test_post_message_targets_the_thread(monkeypatch):
    fake = FakePost({"ok": True})
    monkeypatch.setattr("slack_client.requests.post", fake)

    post_message("C1", "1755000000.000100", "hello", token="xoxb-test")

    sent = fake.calls[0]
    assert sent["json"]["thread_ts"] == "1755000000.000100"
    assert sent["json"]["channel"] == "C1"
    assert sent["headers"]["Authorization"] == "Bearer xoxb-test"


def test_an_ok_false_response_raises(monkeypatch):
    # Slack answers HTTP 200 with {"ok": false} for a missing scope or an unknown
    # channel, so raise_for_status alone would report success on a message that
    # nobody received.
    monkeypatch.setattr(
        "slack_client.requests.post",
        FakePost({"ok": False, "error": "channel_not_found"}),
    )
    with pytest.raises(SlackError, match="channel_not_found"):
        post_message("C1", "1", "hello", token="xoxb-test")


def test_a_network_failure_raises(monkeypatch):
    def refused(*args, **kwargs):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr("slack_client.requests.post", refused)
    with pytest.raises(SlackError):
        post_message("C1", "1", "hello", token="xoxb-test")


# --- the endpoint ------------------------------------------------------------

MENTION_PAYLOAD = {
    "type": "event_callback",
    "event_id": "Ev0001",
    "event": {
        "type": "app_mention",
        "channel": "C1",
        "ts": "1755000000.000100",
        "text": "<@U08ABC123> why is appsrv disk filling up?",
    },
}


@pytest.fixture
def slack_on(monkeypatch):
    """Activate the route. conftest forces SLACK_ENABLED off for every other test."""
    import slack_events

    monkeypatch.setattr(slack_events, "SLACK_ENABLED", True)
    monkeypatch.setattr(slack_events, "SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setattr(slack_events, "SLACK_BOT_TOKEN", "xoxb-test")


@pytest.fixture
def spawned(monkeypatch):
    """Observe dispatch without running the worker."""
    calls = []
    monkeypatch.setattr(app_module, "spawn", calls.append)
    return calls


def signed(payload: dict, secret: str = SECRET):
    body = json.dumps(payload).encode("utf-8")
    timestamp = str(int(time.time()))
    return client.post(
        "/slack/events",
        content=body,  # not json=, or httpx re-serialises and breaks the signature
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": sign(body, timestamp, secret),
        },
    )


def test_a_signed_mention_is_accepted_and_dispatched(slack_on, spawned):
    response = signed(MENTION_PAYLOAD)

    assert response.status_code == 200
    assert spawned[0].question == "why is appsrv disk filling up?"
    assert spawned[0].thread_ts == "1755000000.000100"


def test_an_unsigned_request_is_401(slack_on, spawned):
    response = client.post("/slack/events", content=b"{}")

    assert response.status_code == 401
    assert spawned == []


def test_a_forged_signature_is_401(slack_on, spawned):
    assert signed(MENTION_PAYLOAD, secret="wrong-secret").status_code == 401
    assert spawned == []


def test_the_url_verification_handshake_echoes_the_challenge(slack_on, spawned):
    response = signed({"type": "url_verification", "challenge": "3eZbrw1aB2Cd"})

    assert response.json()["challenge"] == "3eZbrw1aB2Cd"
    assert spawned == []


def test_a_retried_event_does_not_start_a_second_job(slack_on, spawned):
    assert signed(MENTION_PAYLOAD).status_code == 200
    assert signed(MENTION_PAYLOAD).status_code == 200

    # Three retries at 195 seconds each is what this prevents.
    assert len(spawned) == 1


def test_a_non_mention_event_is_ignored(slack_on, spawned):
    payload = {
        "type": "event_callback",
        "event_id": "Ev0002",
        "event": {"type": "message", "channel": "C1", "ts": "1", "text": "hello"},
    }
    assert signed(payload).status_code == 200
    assert spawned == []


def test_an_unparseable_body_is_200_not_500(slack_on, spawned):
    body = b"not json at all"
    timestamp = str(int(time.time()))
    response = client.post(
        "/slack/events",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": sign(body, timestamp),
        },
    )

    # A 500 would make Slack retry a body that can never parse.
    assert response.status_code == 200
    assert spawned == []


def test_the_route_is_404_when_slack_is_not_configured(spawned):
    # No slack_on fixture: conftest left SLACK_ENABLED false and no secrets are set.
    # 404 rather than 401 -- the route does not exist, so there is nothing to sign.
    assert signed(MENTION_PAYLOAD).status_code == 404
    assert spawned == []


# --- the worker --------------------------------------------------------------

SOURCE = app_module.Source(
    marker=1, source="disk-pressure.md", chunk_index=2, score=0.81
)
MENTION = Mention(
    channel="C1",
    thread_ts="1755000000.000100",
    question="why is appsrv disk filling up?",
)


def answered(text, sources=(), answer_source="runbooks"):
    return app_module.AskResponse(
        answer=text,
        sources=list(sources),
        grounded=bool(sources),
        answer_source=answer_source,
    )


@pytest.fixture
def posts(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        app_module,
        "post_message",
        lambda channel, thread_ts, text: recorded.append((channel, thread_ts, text)),
    )
    return recorded


def run_worker(mention=MENTION):
    asyncio.run(app_module.answer_and_post(mention))


def test_the_worker_acks_first_then_answers(posts, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "answer_question",
        lambda *a, **k: answered("The root filesystem is 79% full [1].", [SOURCE]),
    )

    run_worker()

    assert posts[0][2] == ACK_TEXT
    assert "79% full" in posts[1][2]
    # Both land in the thread, never in the channel.
    assert all(post[1] == MENTION.thread_ts for post in posts)


def test_a_short_question_is_nudged_without_an_llm_call(posts, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "answer_question", lambda *a, **k: calls.append(1))

    run_worker(Mention(channel="C1", thread_ts="t1", question="disk?"))

    assert posts == [("C1", "t1", TOO_SHORT)]
    assert calls == []  # a 195-second job is not spent on six characters


@pytest.mark.parametrize(
    "error,expected",
    [
        (EmptyIndexError("collection is empty"), INDEX_EMPTY),
        (UpstreamError("ollama said no", 502), MODEL_DOWN),
        (ValueError("something else entirely"), FAILED),
    ],
)
def test_every_failure_still_posts_something(posts, monkeypatch, error, expected):
    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(app_module, "answer_question", boom)

    run_worker()

    # At 195 seconds a silent bot is indistinguishable from a broken one.
    assert posts[-1][2] == expected


def test_a_refusal_is_posted_and_not_remembered(posts, monkeypatch):
    import sessions

    monkeypatch.setattr(
        app_module,
        "answer_question",
        lambda *a, **k: answered(app_module.NOT_COVERED, answer_source="none"),
    )

    run_worker()

    assert posts[-1][2] == app_module.NOT_COVERED
    # A turn that found nothing would only dilute the next retrieval query.
    assert sessions.history(MENTION.thread_ts, now=time.time()) == []


def test_an_answered_turn_is_remembered(posts, monkeypatch):
    import sessions

    monkeypatch.setattr(
        app_module,
        "answer_question",
        lambda *a, **k: answered("79% full [1].", [SOURCE]),
    )

    run_worker()

    turns = sessions.history(MENTION.thread_ts, now=time.time())
    assert [turn.question for turn in turns] == [MENTION.question]


def test_the_second_turn_receives_the_first_as_history(posts, monkeypatch):
    seen = []

    def spy(question, k=None, turns=()):
        seen.append(list(turns))
        return answered("79% full [1].", [SOURCE])

    monkeypatch.setattr(app_module, "answer_question", spy)

    run_worker()
    run_worker(
        Mention(
            channel="C1",
            thread_ts=MENTION.thread_ts,
            question="what's the command to clear it?",
        )
    )

    assert seen[0] == []
    assert [turn.question for turn in seen[1]] == [MENTION.question]


def test_sources_render_as_slack_mrkdwn(posts, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "answer_question",
        lambda *a, **k: answered("79% full [1].", [SOURCE]),
    )

    run_worker()

    # Slack has no [text](url) and italicises with underscores.
    assert "_[1] disk-pressure.md #2 · 0.81_" in posts[-1][2]


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Observed in production: Slack rendered "sh" as the first line of the block.
        ("```sh\ncrictl rmi --prune\n```", "```\ncrictl rmi --prune\n```"),
        (
            "```yaml\ncontainerLogMaxSize: 50Mi\n```",
            "```\ncontainerLogMaxSize: 50Mi\n```",
        ),
        ("```\nalready bare\n```", "```\nalready bare\n```"),
        # Two fenced blocks in one answer, which is the common shape.
        (
            "```sh\na\n```\ntext\n```yaml\nb: 1\n```",
            "```\na\n```\ntext\n```\nb: 1\n```",
        ),
        # Inline code is a different construct and Slack renders it correctly.
        (
            "set `containerLogMaxSize` in the kubelet config",
            "set `containerLogMaxSize` in the kubelet config",
        ),
        # Prose that merely mentions backticks must not be rewritten.
        ("use ``` to open a block", "use ``` to open a block"),
    ],
)
def test_strip_fence_languages(raw, expected):
    assert app_module.strip_fence_languages(raw) == expected


def test_the_posted_answer_has_no_fence_languages(posts, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "answer_question",
        lambda *a, **k: answered(
            "Reclaim space [1]:\n```sh\njournalctl --vacuum-size=200M\n```", [SOURCE]
        ),
    )

    run_worker()

    assert "```sh" not in posts[-1][2]
    assert "journalctl --vacuum-size=200M" in posts[-1][2]


def test_a_failed_slack_post_does_not_crash_the_worker(monkeypatch):
    def boom(*args, **kwargs):
        raise SlackError("channel_not_found")

    monkeypatch.setattr(app_module, "post_message", boom)
    monkeypatch.setattr(
        app_module,
        "answer_question",
        lambda *a, **k: answered("79% full [1].", [SOURCE]),
    )

    run_worker()  # must not raise
