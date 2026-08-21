import pytest
from fastapi.testclient import TestClient

import app as app_module
import risk
import triage
from app import app, body_size_error
from errors import TriageProviderError
from triage import TriageResult

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# One real Bandit finding, in Bandit's own shape -- scanners.py is what turns it into a
# Finding, and going through that rather than constructing a Finding directly is what
# makes this an endpoint test instead of a mock test.
ENVELOPE = {
    "repo": "git@github.com:crypticani/autonomous-infra-labs",
    "commit": "0123456789abcdef",
    "branch": "main",
    "scans": {
        "bandit": {
            "results": [
                {
                    "test_id": "B105",
                    "issue_text": "Possible hardcoded password",
                    "issue_severity": "LOW",
                    "filename": "./services/x/settings.py",
                    "line_number": 3,
                    "code": "2 import os\n3 password = 'hunter2'\n4 print(password)",
                }
            ]
        }
    },
}


@pytest.fixture
def client(monkeypatch):
    """A fresh process, effectively: run state and rate-limit buckets are module-level
    dicts, so without this one test's runs leak into the next one's counts."""
    monkeypatch.setattr(app_module, "TOKENS", {TOKEN})
    monkeypatch.setattr(app_module, "_runs", {})
    monkeypatch.setattr(app_module, "_starts", {})
    return TestClient(app)


def _fake_triage(priority="critical"):
    """Judges every finding it is handed, at whatever priority the test needs.

    TestClient runs background tasks before returning from the POST, so without this the
    happy-path test would make real Ollama calls -- minutes each, and a 502 on any
    machine without a backend running.
    """

    def triage_findings(findings, provider=None, batch_size=None):
        return [
            TriageResult(
                fingerprint=finding.fingerprint,
                priority=priority,
                exploitability="high",
                impact="high",
                explanation="a credential in source, readable by anyone with the repo",
                confidence=0.9,
            )
            for finding in findings
        ]

    return triage_findings


def test_a_run_goes_from_202_to_a_verdict(client, monkeypatch):
    monkeypatch.setattr(app_module, "triage_findings", _fake_triage("critical"))

    # Explicit threshold rather than the deploy default: a .env with its own
    # ST_RISK_THRESHOLD would otherwise decide whether this test passes.
    response = client.post(
        "/triage", json={**ENVELOPE, "risk_threshold": 40}, headers=AUTH
    )
    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "pending"
    assert accepted["findings"] == 1

    run = client.get(f"/triage/{accepted['run_id']}", headers=AUTH).json()
    assert run["status"] == "done"
    assert run["triaged"] == 1
    assert run["risk"]["score"] == 40
    assert run["risk"]["verdict"] == "fail"
    assert run["repo"] == ENVELOPE["repo"]

    # The join, end to end: the comment needs a rule id and a path, and the only place
    # those exist is the Finding the judgment was about.
    assert run["top"] == [
        {
            "fingerprint": run["top"][0]["fingerprint"],
            "priority": "critical",
            "explanation": "a credential in source, readable by anyone with the repo",
            "scanner": "bandit",
            "rule_id": "B105",
            "title": "Possible hardcoded password",
            "target": "./services/x/settings.py",
            "line": 3,
        }
    ]


def test_the_per_request_threshold_reaches_the_verdict(client, monkeypatch):
    monkeypatch.setattr(app_module, "triage_findings", _fake_triage("critical"))

    response = client.post(
        "/triage", json={**ENVELOPE, "risk_threshold": 90}, headers=AUTH
    )
    run_id = response.json()["run_id"]

    run = client.get(f"/triage/{run_id}", headers=AUTH).json()
    assert run["risk"]["threshold"] == 90
    assert run["risk"]["verdict"] == "pass"


def test_a_provider_failure_is_a_recorded_run_not_a_lost_one(client, monkeypatch):
    """The failure mode this is really about: a run that stays `pending` forever leaves
    the polling CI job spinning until its own timeout with nothing to read."""

    def exploding(findings, provider=None, batch_size=None):
        raise TriageProviderError("the model took too long", 504, provider="ollama")

    monkeypatch.setattr(app_module, "triage_findings", exploding)

    run_id = client.post("/triage", json=ENVELOPE, headers=AUTH).json()["run_id"]
    run = client.get(f"/triage/{run_id}", headers=AUTH).json()
    assert run["status"] == "failed"
    assert "ollama" in run["error"]
    assert run["risk"] is None


def test_a_malformed_envelope_is_rejected_before_the_202(client):
    """Parsing happens synchronously so this is a 422 the caller can read, not a
    `failed` run it has to poll for."""
    assert client.post("/triage", json={"scans": {}}, headers=AUTH).status_code == 422


def test_an_empty_envelope_is_a_clean_pass(client, monkeypatch):
    """A repo with no findings is not an error -- and neither is a caller that sends no
    scanners at all, per Day 22's partial-envelope rule."""
    monkeypatch.setattr(app_module, "triage_findings", _fake_triage())

    run_id = client.post(
        "/triage", json={"repo": "empty", "scans": {}}, headers=AUTH
    ).json()["run_id"]
    run = client.get(f"/triage/{run_id}", headers=AUTH).json()
    assert run["status"] == "done"
    assert (run["risk"]["score"], run["risk"]["verdict"]) == (0, "pass")


def test_an_unknown_run_is_a_404(client):
    assert client.get("/triage/deadbeef", headers=AUTH).status_code == 404


def test_both_triage_routes_require_a_token(client):
    wrong = {"Authorization": "Bearer wrong"}
    assert client.post("/triage", json=ENVELOPE).status_code == 401
    assert client.get("/triage/anything").status_code == 401
    assert client.post("/triage", json=ENVELOPE, headers=wrong).status_code == 401


def test_health_is_open_and_reports_the_loaded_policy(client):
    """Against the module's own values, not literals: the point of the block is that it
    reports what this *process* loaded, and appsrv's .env overrides image defaults."""
    body = client.get("/health").json()
    assert body["policy"]["risk_threshold"] == risk.THRESHOLD
    assert body["policy"]["batch_size"] == triage.BATCH_SIZE
    assert body["auth"] == "1 token(s)"


def test_health_flags_a_deploy_with_no_tokens(client, monkeypatch):
    monkeypatch.setattr(app_module, "TOKENS", set())
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert any("ST_API_TOKENS" in issue for issue in body["issues"])


def test_the_rate_limit_refuses_the_next_run(client, monkeypatch):
    monkeypatch.setattr(app_module, "triage_findings", _fake_triage())
    monkeypatch.setattr(app_module, "MAX_RUNS_PER_HOUR", 2)

    for _ in range(2):
        assert client.post("/triage", json=ENVELOPE, headers=AUTH).status_code == 202
    refused = client.post("/triage", json=ENVELOPE, headers=AUTH)
    assert refused.status_code == 429
    assert "limit is 2" in refused.json()["detail"]


def test_the_rate_limit_is_per_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "TOKENS", {TOKEN, "other-token"})
    monkeypatch.setattr(app_module, "triage_findings", _fake_triage())
    monkeypatch.setattr(app_module, "MAX_RUNS_PER_HOUR", 1)

    other = {"Authorization": "Bearer other-token"}
    assert client.post("/triage", json=ENVELOPE, headers=AUTH).status_code == 202
    assert client.post("/triage", json=ENVELOPE, headers=AUTH).status_code == 429
    assert client.post("/triage", json=ENVELOPE, headers=other).status_code == 202


def test_an_oversized_envelope_is_refused_on_content_length(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_BODY_BYTES", 100)
    response = client.post("/triage", json=ENVELOPE, headers=AUTH)
    assert response.status_code == 413


def test_a_body_with_no_declared_length_is_refused():
    """Checked directly: httpx always sets Content-Length, so the chunked case cannot be
    reached through the client at all."""
    assert body_size_error(None)[0] == 411
    assert body_size_error("not-a-number")[0] == 413
    assert body_size_error("10") is None


def test_old_runs_are_evicted(client, monkeypatch):
    monkeypatch.setattr(app_module, "triage_findings", _fake_triage())
    monkeypatch.setattr(app_module, "MAX_RUNS", 2)
    monkeypatch.setattr(app_module, "MAX_RUNS_PER_HOUR", 10)

    ids = [
        client.post("/triage", json=ENVELOPE, headers=AUTH).json()["run_id"]
        for _ in range(3)
    ]
    assert client.get(f"/triage/{ids[0]}", headers=AUTH).status_code == 404
    assert client.get(f"/triage/{ids[2]}", headers=AUTH).status_code == 200
