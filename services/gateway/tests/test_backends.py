import json

import pytest

import backends
from backends import BACKENDS, BY_NAME, NAMES


def test_the_table_is_the_only_list_of_service_names():
    """router.py builds its schema enum from NAMES, so a name that exists in one place and
    not the other is a service the model can pick and the proxy cannot reach."""
    assert NAMES == tuple(b.name for b in BACKENDS)
    assert set(NAMES) == set(BY_NAME)
    assert len(NAMES) == 4


def test_every_backend_declares_a_hint_exactly_when_it_needs_something():
    """The two halves of a `needs_input` reply. A `needs` with no hint tells a caller a
    field name and not where to get one, which is the unhelpful half of a refusal."""
    for backend in BACKENDS:
        assert bool(backend.needs) == bool(backend.hint), backend.name


def test_the_copilot_takes_the_question_and_nothing_else():
    body = BY_NAME["knowledge-copilot"].body("how do I drain a node", "")
    # No `k`: the copilot's own default is the one place that number should live.
    assert body == {"question": "how do I drain a node"}


def test_the_log_analyzer_takes_the_attachment_verbatim():
    log = "2026-08-25 ERROR OutOfMemoryError in checkout-7d9f"
    assert BY_NAME["log-analyzer"].body("what broke", log) == {"raw_log": log}


def test_the_agent_takes_a_parsed_alert():
    alert = {"labels": {"alertname": "KubePodCrashLooping", "namespace": "sandbox"}}
    body = BY_NAME["self-healing-agent"].body("what do I do", json.dumps(alert))
    assert body == {"alert": alert}


def test_triage_forwards_the_scan_envelope_unchanged():
    """scan.sh already emits exactly what POST /triage takes, repo and all. Re-assembling
    it here would only be a chance to assemble it differently."""
    envelope = {"repo": "git@github.com:x/y", "commit": "abc", "scans": {"bandit": {}}}
    assert BY_NAME["security-triage"].body("is this safe", json.dumps(envelope)) == (
        envelope
    )


def test_a_json_backend_raises_rather_than_forwarding_garbage():
    with pytest.raises(json.JSONDecodeError):
        BY_NAME["self-healing-agent"].body("what do I do", "not json at all")


def test_a_plural_token_variable_yields_its_first_entry(monkeypatch):
    """ST_API_TOKENS is comma-separated because triage issues one per repo; the others are
    singular. Splitting always means no per-backend flag saying which shape to expect.
    """
    monkeypatch.setenv("ST_API_TOKENS", " first-token , second-token ")
    assert BY_NAME["security-triage"].token() == "first-token"

    monkeypatch.setenv("KC_API_TOKEN", "only-one")
    assert BY_NAME["knowledge-copilot"].token() == "only-one"


def test_the_log_analyzer_gets_no_authorization_header():
    """It has no auth of its own, and an Authorization header it does not read is one more
    thing to wonder about in a tcpdump."""
    assert BY_NAME["log-analyzer"].token() == ""
    assert BY_NAME["log-analyzer"].headers() == {}


def test_an_unset_token_is_no_header_rather_than_an_empty_bearer(monkeypatch):
    monkeypatch.setenv("SHA_API_TOKEN", "")
    assert BY_NAME["self-healing-agent"].headers() == {}


def test_the_url_prefers_the_environment_over_the_compose_default(monkeypatch):
    monkeypatch.setenv("GW_KNOWLEDGE_COPILOT_URL", "http://localhost:7100")
    assert BY_NAME["knowledge-copilot"].url() == "http://localhost:7100"

    monkeypatch.delenv("GW_KNOWLEDGE_COPILOT_URL")
    assert BY_NAME["knowledge-copilot"].url() == "http://knowledge-copilot:7100"


def test_the_catalogue_describes_every_service_to_the_router():
    """A backend missing from the prompt is one the model can never route to, and the
    schema would still happily let it name it."""
    catalogue = backends.catalogue()
    for backend in BACKENDS:
        assert backend.name in catalogue
        assert backend.answers in catalogue
