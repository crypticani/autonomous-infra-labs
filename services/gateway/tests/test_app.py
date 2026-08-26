import json

import pytest
import requests
from fastapi.testclient import TestClient

import app as app_module
from app import app, body_size_error
from conftest import FakeResponse
from errors import GatewayProviderError
from router import NONE, Route

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

ALERT = {"labels": {"alertname": "KubePodCrashLooping", "namespace": "sandbox"}}
ENVELOPE = {"repo": "git@github.com:x/y", "commit": "abc", "scans": {"bandit": {}}}


@pytest.fixture
def client(monkeypatch):
    """A fresh process, effectively: the rate-limit buckets are a module-level dict, so
    without this one test's asks leak into the next one's counts."""
    monkeypatch.setattr(app_module, "TOKENS", {TOKEN})
    monkeypatch.setattr(app_module, "_starts", {})
    return TestClient(app)


@pytest.fixture
def routed(monkeypatch):
    """Decide what the router says, without a model."""

    def install(
        service="knowledge-copilot", confidence="high", reason="a runbook question"
    ):
        route = Route(reason=reason, service=service, confidence=confidence)
        monkeypatch.setattr(app_module, "classify", lambda q, a="": route)
        return route

    return install


@pytest.fixture
def upstream(monkeypatch):
    """Install a fake backend and hand back the list of calls it received."""
    calls = []

    def install(response):
        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append({"url": url, "json": json, "headers": headers or {}})
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(app_module.requests, "post", fake_post)
        return calls

    return install


# --- the happy path ---


def test_a_routed_question_is_forwarded_and_attributed(client, routed, upstream):
    routed("knowledge-copilot", "high", "asks what the documented procedure is")
    calls = upstream(FakeResponse(200, {"answer": "drain it first", "grounded": True}))

    body = client.post(
        "/ask", json={"question": "how do I take a node out of service"}, headers=AUTH
    ).json()

    assert body["outcome"] == "answered"
    assert body["service"] == "knowledge-copilot"
    assert body["answer"] == {"answer": "drain it first", "grounded": True}
    assert body["attributed_to"] == "knowledge-copilot POST /ask-runbook"
    # The routing rides along on a success too, which is what makes a misroute legible in
    # the answer rather than hidden behind it.
    assert body["confidence"] == "high"
    assert body["reason"] == "asks what the documented procedure is"
    assert body["detail"] is None

    assert calls[0]["url"] == "http://knowledge-copilot.test:7100/ask-runbook"
    assert calls[0]["json"] == {"question": "how do I take a node out of service"}
    # The caller's edge token is not the token the backend sees.
    assert calls[0]["headers"]["Authorization"] == "Bearer kc-test-token"


def test_an_attachment_reaches_the_backend_that_needed_it(client, routed, upstream):
    routed("log-analyzer", "high", "asks what a log line means")
    calls = upstream(FakeResponse(200, {"severity": "critical"}))
    log = "2026-08-25 12:04:11 ERROR OutOfMemoryError in checkout-7d9f"

    body = client.post(
        "/ask",
        json={"question": "why did checkout start 500ing", "attachment": log},
        headers=AUTH,
    ).json()

    assert body["outcome"] == "answered"
    assert calls[0]["json"] == {"raw_log": log}
    # log-analyzer has no auth of its own, and an Authorization header it does not read is
    # one more thing to wonder about in a tcpdump.
    assert "Authorization" not in calls[0]["headers"]


def test_a_json_attachment_is_parsed_for_the_backend_that_wants_an_object(
    client, routed, upstream
):
    routed("self-healing-agent", "high", "asks what to do about a firing alert")
    calls = upstream(FakeResponse(200, {"summary": "the pod is OOMKilling"}))

    client.post(
        "/ask",
        json={
            "question": "what should I do about this",
            "attachment": json.dumps(ALERT),
        },
        headers=AUTH,
    )

    assert calls[0]["json"] == {"alert": ALERT}
    assert calls[0]["headers"]["Authorization"] == "Bearer sha-test-token"


def test_triage_answering_202_is_accepted_and_not_answered(client, routed, upstream):
    """Calling a pending run `answered` would be a lie the response body immediately
    contradicts."""
    routed("security-triage", "high", "asks whether scanner findings are shippable")
    upstream(FakeResponse(202, {"run_id": "9fd2c1a4b0e7", "status": "pending"}))

    body = client.post(
        "/ask",
        json={
            "question": "is this branch safe to ship",
            "attachment": json.dumps(ENVELOPE),
        },
        headers=AUTH,
    ).json()

    assert body["outcome"] == "accepted"
    assert body["answer"]["run_id"] == "9fd2c1a4b0e7"
    # A path that goes back through this gateway, not the backend's own address: the
    # caller has one token and one host, which is the point of the thing.
    assert body["poll"] == "/s/security-triage/triage/9fd2c1a4b0e7"


# --- the two refusals, which come from different places ---


def test_the_model_declining_is_a_200_that_names_no_service(client, routed, upstream):
    """A declared refusal is a 200 with a field, like triage's needs_human."""
    routed(NONE, "high", "could be the log analyzer or the cluster agent")
    upstream(FakeResponse(200, {"never": "called"}))

    response = client.post(
        "/ask", json={"question": "something is wrong with checkout"}, headers=AUTH
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "unroutable"
    assert body["service"] is None
    assert body["answer"] is None
    # The model's sentence survives the decline; it is the useful half.
    assert body["reason"] == "could be the log analyzer or the cluster agent"
    assert "could not place" in body["detail"]


def test_a_low_confidence_route_is_declined_and_says_what_it_would_have_picked(
    client, routed
):
    routed("security-triage", "low", "mentions shipping")
    body = client.post(
        "/ask", json={"question": "should I ship this or not"}, headers=AUTH
    ).json()

    assert body["outcome"] == "unroutable"
    # Named, not hidden: the caller can decide the router was right and retry explicitly.
    assert body["service"] == "security-triage"
    assert "low" in body["detail"]


def test_the_right_service_with_no_attachment_asks_for_one(client, routed, upstream):
    """log-analyzer's question, and log-analyzer analyses text it is handed."""
    routed("log-analyzer", "high", "asks why a service returned errors")
    calls = upstream(FakeResponse(200, {"never": "called"}))

    body = client.post(
        "/ask", json={"question": "why did checkout start 500ing at 3am"}, headers=AUTH
    ).json()

    assert body["outcome"] == "needs_input"
    assert body["service"] == "log-analyzer"
    assert body["needs"] == "raw_log"
    assert "attach the log text" in body["detail"]
    assert body["answer"] is None
    # Nothing was fabricated and nothing was sent.
    assert calls == []


def test_the_copilot_never_asks_for_an_attachment(client, routed, upstream):
    """It is the one backend a sentence is enough for, so `needs` is None and the check
    that would refuse never fires."""
    routed("knowledge-copilot", "high")
    upstream(FakeResponse(200, {"answer": "here"}))

    body = client.post(
        "/ask", json={"question": "what is our incident severity scale"}, headers=AUTH
    ).json()
    assert body["outcome"] == "answered"


def test_an_unparseable_json_attachment_is_a_400_naming_the_field(
    client, routed, upstream
):
    routed("self-healing-agent", "high")
    calls = upstream(FakeResponse(200, {"never": "called"}))

    response = client.post(
        "/ask",
        json={"question": "what should I do here", "attachment": "not json at all"},
        headers=AUTH,
    )

    assert response.status_code == 400
    assert "alert" in response.json()["detail"]
    assert calls == []


# --- backends going wrong ---


def test_an_unreachable_backend_is_a_503_that_names_it(client, routed, upstream):
    routed("knowledge-copilot", "high")
    upstream(requests.exceptions.ConnectionError("connection refused"))

    response = client.post(
        "/ask", json={"question": "how do I drain a node"}, headers=AUTH
    )

    assert response.status_code == 503
    body = response.json()
    assert body["outcome"] == "failed"
    assert "knowledge-copilot is unreachable" in body["detail"]
    # Still attributed, so the caller knows which of the four was down.
    assert body["attributed_to"] == "knowledge-copilot POST /ask-runbook"


def test_a_backend_status_is_mirrored_rather_than_flattened(client, routed, upstream):
    """A 422 means the attachment was wrong and a 429 means try later. Those are different
    instructions and only the real status carries them."""
    routed("log-analyzer", "high")
    upstream(FakeResponse(422, {"detail": "raw_log is too short"}))

    response = client.post(
        "/ask",
        json={"question": "what does this mean", "attachment": "short"},
        headers=AUTH,
    )

    assert response.status_code == 422
    body = response.json()
    assert body["outcome"] == "failed"
    assert body["answer"] == {"detail": "raw_log is too short"}


def test_a_non_json_body_from_upstream_does_not_become_a_stack_trace(
    client, routed, upstream
):
    """A 502 page from an intermediary is not JSON, and letting .json() raise would turn
    somebody else's outage into a traceback in this service's logs."""
    routed("knowledge-copilot", "high")
    upstream(FakeResponse(502, None, text="<html>Bad Gateway</html>"))

    response = client.post(
        "/ask", json={"question": "how do I drain a node"}, headers=AUTH
    )

    assert response.status_code == 502
    assert "Bad Gateway" in response.json()["answer"]["raw"]


def test_the_router_itself_failing_is_the_provider_status(client, monkeypatch):
    def explode(question, attachment=""):
        raise GatewayProviderError("the model is down", 503, provider="ollama")

    monkeypatch.setattr(app_module, "classify", explode)

    response = client.post(
        "/ask", json={"question": "how do I drain a node"}, headers=AUTH
    )
    assert response.status_code == 503
    assert "ollama" in response.json()["detail"]


# --- the edge controls ---


def test_ask_requires_a_token(client):
    assert client.post("/ask", json={"question": "how do I drain"}).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert (
        client.post("/ask", json={"question": "how do I drain"}, headers=wrong)
    ).status_code == 401


def test_a_question_too_short_to_route_is_a_422(client):
    assert (
        client.post("/ask", json={"question": "help"}, headers=AUTH).status_code == 422
    )


def test_the_body_cap_reads_content_length_not_the_parsed_body():
    """By the time a body is a dict the memory it exists to refuse is already spent."""
    assert body_size_error(None) == (411, "Content-Length is required on a POST")
    assert body_size_error("999999999")[0] == 413
    assert body_size_error("not-a-number")[0] == 413
    assert body_size_error("2048") is None


def test_the_rate_limit_refuses_the_next_ask(client, routed, upstream, monkeypatch):
    """It exists because /ask spends a model call before any backend's own limit gets a
    say, so without it one token can burn the router freely."""
    routed("knowledge-copilot", "high")
    upstream(FakeResponse(200, {"answer": "here"}))
    monkeypatch.setattr(app_module, "MAX_ASKS_PER_HOUR", 2)

    for _ in range(2):
        assert (
            client.post(
                "/ask", json={"question": "how do I drain a node"}, headers=AUTH
            ).status_code
            == 200
        )

    refused = client.post(
        "/ask", json={"question": "how do I drain a node"}, headers=AUTH
    )
    assert refused.status_code == 429


# --- the passthrough ---


def test_the_proxy_swaps_the_edge_token_for_the_backends_own(client, monkeypatch):
    seen = {}

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        seen.update(method=method, url=url, json=json, params=params, headers=headers)
        return FakeResponse(200, {"status": "done", "risk": {"verdict": "pass"}})

    monkeypatch.setattr(app_module.requests, "request", fake_request)

    response = client.get("/s/security-triage/triage/9fd2c1a4b0e7", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["risk"]["verdict"] == "pass"
    assert seen["url"] == "http://security-triage.test:7300/triage/9fd2c1a4b0e7"
    assert seen["headers"]["Authorization"] == "Bearer st-test-token"


def test_the_proxy_forwards_a_post_body_and_the_query_string(client, monkeypatch):
    seen = {}

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        seen.update(method=method, url=url, json=json, params=params)
        return FakeResponse(202, {"run_id": "abc123", "status": "pending"})

    monkeypatch.setattr(app_module.requests, "request", fake_request)

    response = client.post(
        "/s/security-triage/triage?dry_run=1", json=ENVELOPE, headers=AUTH
    )

    assert response.status_code == 202
    assert seen["method"] == "POST"
    assert seen["json"] == ENVELOPE
    assert seen["params"] == {"dry_run": "1"}


def test_the_proxy_404s_a_service_that_does_not_exist(client):
    response = client.get("/s/grafana/api/dashboards", headers=AUTH)
    assert response.status_code == 404
    assert "grafana" in response.json()["detail"]


def test_the_proxy_requires_a_token(client):
    assert client.get("/s/security-triage/health").status_code == 401


def test_the_proxy_does_not_shadow_the_gateways_own_routes(client, monkeypatch):
    """Under /s/ so it cannot shadow the gateway's own routes."""
    monkeypatch.setattr(
        app_module.requests, "get", lambda url, timeout=None: FakeResponse(200, {})
    )
    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200


# --- aggregated health ---


def _health_stub(statuses: dict[str, dict], ollama=("test-router-model",)):
    """One canned /health per backend address, plus Ollama's /api/tags.

    /health asks the model backend whether it is up and whether the router's model is
    pulled, so a stub that only answers the four backends leaves that call going to a real
    address. `ollama` is the list of pulled model names; pass an Exception to make the
    backend unreachable, or () to make it reachable with nothing pulled.
    """

    def fake_get(url, timeout=None):
        if url.endswith("/api/tags"):
            if isinstance(ollama, Exception):
                raise ollama
            return FakeResponse(200, {"models": [{"name": n} for n in ollama]})
        for host, body in statuses.items():
            if host in url:
                if isinstance(body, Exception):
                    raise body
                return FakeResponse(200, body)
        return FakeResponse(200, {"status": "healthy", "issues": []})

    return fake_get


def test_health_reports_an_unreachable_model_backend(client, monkeypatch):
    """Constructing a provider does no I/O, so without /api/tags this calls an unreachable
    model healthy."""
    monkeypatch.setattr(
        app_module.requests,
        "get",
        _health_stub({}, ollama=requests.exceptions.ConnectionError("refused")),
    )

    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert any("Ollama unreachable" in issue for issue in body["issues"])
    # Every backend was fine, so this is the only complaint.
    assert all(b["status"] == "healthy" for b in body["backends"])


def test_health_reports_a_router_model_that_is_not_pulled(client, monkeypatch):
    """The failure mode of setting GW_OLLAMA_MODEL to something plausible: Ollama answers,
    the pod is up, and every /ask 502s."""
    monkeypatch.setattr(
        app_module.requests, "get", _health_stub({}, ollama=("some-other-model",))
    )

    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert any("is not pulled" in issue for issue in body["issues"])


def test_a_model_pulled_without_a_tag_still_counts_as_pulled(client, monkeypatch):
    """Ollama reports `foo:latest` for a model pulled as bare `foo`, so a name-equality
    check alone calls a present model missing."""
    monkeypatch.setattr(
        app_module.requests,
        "get",
        _health_stub({}, ollama=("test-router-model:latest",)),
    )

    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert body["issues"] == []


def test_health_is_open_and_reports_every_backend(client, monkeypatch):
    """A gateway that says healthy while three of the four services behind it are down is
    reporting on the wrong thing."""
    monkeypatch.setattr(app_module.requests, "get", _health_stub({}))

    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "healthy"
    assert {b["service"] for b in body["backends"]} == {
        "log-analyzer",
        "knowledge-copilot",
        "self-healing-agent",
        "security-triage",
    }
    assert all(b["latency_ms"] >= 0 for b in body["backends"])
    assert body["policy"]["route_on"] == "medium"


def test_one_degraded_backend_degrades_the_gateway(client, monkeypatch):
    monkeypatch.setattr(
        app_module.requests,
        "get",
        _health_stub(
            {
                "knowledge-copilot.test": {
                    "status": "degraded",
                    "issues": ["collection 'runbooks' is empty; run ingest.py"],
                }
            }
        ),
    )

    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert any("knowledge-copilot is degraded" in i for i in body["issues"])

    copilot = next(b for b in body["backends"] if b["service"] == "knowledge-copilot")
    # The backend's own issues, carried up rather than summarised away.
    assert copilot["issues"] == ["collection 'runbooks' is empty; run ingest.py"]


def test_a_backend_answering_200_while_calling_itself_degraded_is_believed(
    client, monkeypatch
):
    """All four answer 200 while reporting degraded, which is the case that matters and the
    one a status-code check would miss."""
    monkeypatch.setattr(
        app_module.requests,
        "get",
        _health_stub(
            {"log-analyzer.test": {"status": "degraded", "issues": ["no key"]}}
        ),
    )

    body = client.get("/health").json()
    analyzer = next(b for b in body["backends"] if b["service"] == "log-analyzer")
    assert analyzer["http"] == 200
    assert analyzer["status"] == "degraded"


def test_an_unreachable_backend_is_reported_not_raised(client, monkeypatch):
    monkeypatch.setattr(
        app_module.requests,
        "get",
        _health_stub(
            {"self-healing-agent.test": requests.exceptions.ConnectTimeout("timed out")}
        ),
    )

    response = client.get("/health")
    # Degraded, never unhealthy: /ask still routes and still declines correctly with every
    # backend down, and a container that reports itself dead gets restarted, which fixes
    # nothing when the fault is somebody else's.
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"

    agent = next(b for b in body["backends"] if b["service"] == "self-healing-agent")
    assert agent["status"] == "unreachable"
    assert agent["http"] is None


def test_health_flags_a_deploy_with_no_tokens(client, monkeypatch):
    """A deploy that forgot GW_API_TOKENS is visible here rather than quietly serving every
    service behind it to the internet."""
    monkeypatch.setattr(app_module, "TOKENS", set())
    monkeypatch.setattr(app_module.requests, "get", _health_stub({}))

    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["auth"] == "disabled"
    assert any("GW_API_TOKENS" in issue for issue in body["issues"])
