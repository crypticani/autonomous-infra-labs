"""Day 18: the wire format. Every request here is fabricated offline -- which is the
point, since a forged request is exactly what verify_signature exists to reject, and
the only honest way to test that is to forge one.
"""

import hashlib
import hmac
import json
import time
import urllib.parse

import pytest

import approvals
import slack

SECRET = "8f742231b10e8888abcdefff85a5a5a5"

PAYLOAD = {
    "type": "block_actions",
    "user": {"id": "U08ABC123", "username": "aniket"},
    "response_url": "https://hooks.slack.com/actions/T0/1/xyz",
    "actions": [{"action_id": "approve", "value": "abc123def456", "type": "button"}],
}


def form(payload: dict) -> bytes:
    """What Slack actually sends an interactive component to: form-encoded, with the
    whole interaction JSON-encoded under one `payload` key."""
    return urllib.parse.urlencode({"payload": json.dumps(payload)}).encode("utf-8")


def signed(body: bytes, timestamp: str) -> str:
    digest = hmac.new(
        SECRET.encode("utf-8"),
        b"v0:" + timestamp.encode("utf-8") + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return f"v0={digest}"


def test_a_form_encoded_body_verifies_over_its_raw_bytes():
    now = time.time()
    timestamp = str(int(now))
    body = form(PAYLOAD)

    assert slack.verify_signature(body, timestamp, signed(body, timestamp), now, SECRET)


def test_a_tampered_body_does_not_verify():
    """Flipping the decision inside the payload without re-signing. Same length, still
    valid form encoding, and it must not pass -- the signature covers the bytes, not
    the shape."""
    now = time.time()
    timestamp = str(int(now))
    body = form(PAYLOAD)
    signature = signed(body, timestamp)
    tampered = body.replace(b"approve", b"approvE")

    assert tampered != body
    assert not slack.verify_signature(tampered, timestamp, signature, now, SECRET)


def test_a_captured_request_stops_working_once_it_is_stale():
    """The HMAC never expires on its own. Without the age check a request captured
    today stays replayable forever, and replaying this one deletes a pod."""
    timestamp = str(int(time.time()))
    body = form(PAYLOAD)
    signature = signed(body, timestamp)
    much_later = time.time() + slack.MAX_SIGNATURE_AGE + 1

    assert not slack.verify_signature(body, timestamp, signature, much_later, SECRET)


@pytest.mark.parametrize(
    "timestamp, signature",
    [("", "v0=whatever"), (str(int(time.time())), ""), ("not-a-number", "v0=x")],
)
def test_missing_or_unparseable_headers_are_false_not_an_exception(
    timestamp, signature
):
    body = form(PAYLOAD)
    assert not slack.verify_signature(body, timestamp, signature, time.time(), SECRET)


def test_parse_interaction_reads_the_payload_key_rather_than_the_body_as_json():
    interaction = slack.parse_interaction(form(PAYLOAD))

    assert interaction.proposal_id == "abc123def456"
    assert interaction.decision == "approve"
    assert interaction.user == "aniket"
    assert interaction.response_url == "https://hooks.slack.com/actions/T0/1/xyz"


def test_a_user_without_a_username_falls_back_to_the_id():
    """The audit log needs *someone* named. An empty string in the `user` field would
    be worse than a raw Slack id."""
    payload = {**PAYLOAD, "user": {"id": "U08ABC123"}}

    assert slack.parse_interaction(form(payload)).user == "U08ABC123"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"payload=not-json",
        b"nothing=here",
        json.dumps(PAYLOAD).encode("utf-8"),  # JSON, the way /slack/events posts
    ],
)
def test_unparseable_bodies_are_none_rather_than_an_exception(body):
    """None means 'answer 200 and do nothing'. An exception here would be a 500, and a
    500 makes Slack retry a button click."""
    assert slack.parse_interaction(body) is None


def test_an_interaction_that_is_not_an_approval_button_is_ignored():
    payload = {**PAYLOAD, "actions": [{"action_id": "open_runbook", "value": "x"}]}

    assert slack.parse_interaction(form(payload)) is None


def test_both_buttons_carry_the_proposal_id_and_the_args_render_verbatim():
    """The id travels in `value`, so the click that comes back identifies the proposal
    without the handler having to guess from the channel or the message text."""
    proposal = approvals.Proposal(
        id="abc123def456",
        tool="restart_pod",
        args={"namespace": "sandbox", "pod": "checkout-api-7d9f-x2k"},
        summary="checkout-api OOMKilled",
        confidence=0.91,
        created=0.0,
    )

    blocks = slack.blocks_for(proposal)
    actions = [b for b in blocks if b["type"] == "actions"][0]

    assert [e["action_id"] for e in actions["elements"]] == ["approve", "reject"]
    assert [e["value"] for e in actions["elements"]] == ["abc123def456"] * 2
    assert "checkout-api-7d9f-x2k" in json.dumps(blocks)
    assert "0.91" in json.dumps(blocks)


def test_a_proposal_with_no_confidence_still_renders():
    """An incomplete diagnosis has confidence None. Formatting it with :.2f would raise
    here, in the one code path whose job is to show a human what they are approving."""
    proposal = approvals.Proposal(
        id="abc123def456",
        tool="restart_pod",
        args={},
        summary="",
        confidence=None,
        created=0.0,
    )

    assert "unknown" in json.dumps(slack.blocks_for(proposal))
