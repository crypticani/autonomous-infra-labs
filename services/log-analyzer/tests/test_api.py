import json
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
import requests

from log_analyzer import app, llm_provider, LogAnalysis, OllamaProvider

client = TestClient(app)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_ollama_sends_temperature_inside_options(monkeypatch):
    """The bug that made the golden-set eval non-deterministic."""
    sent = {}

    def capture(url, json=None, timeout=None):
        sent.update(json)
        return _FakeResponse(
            {
                "response": LogAnalysis(
                    likely_cause="c", suggested_fix="f", severity="LOW", confidence=0.5
                ).model_dump_json()
            }
        )

    monkeypatch.setattr(requests, "post", capture)
    OllamaProvider().generate("sys", "usr", temperature=0.0)

    assert sent["options"] == {"temperature": 0.0}
    assert "temperature" not in sent, "a top-level temperature is silently ignored"


def test_severity_is_generated_after_the_reasoning_fields():
    """Field order is a behavioural contract here, not formatting."""
    fields = list(LogAnalysis.model_json_schema()["properties"])
    assert fields.index("severity") > fields.index("likely_cause")
    assert fields.index("severity") > fields.index("suggested_fix")
    assert fields.index("confidence") > fields.index("severity")


def test_analyze_log_success(monkeypatch):
    mock_generate = MagicMock(
        return_value=LogAnalysis(
            severity="MEDIUM",
            likely_cause="Mocked timeout",
            suggested_fix="Increase timeout value / add retry",
            confidence=0.9,
        )
    )
    monkeypatch.setattr(llm_provider, "generate", mock_generate)

    response = client.post(
        "/analyze-log",
        json={"raw_log": "java.net.SocketTimeoutException: Read timed out"},
    )

    assert response.status_code == 200
    assert response.json()["severity"] == "MEDIUM"
    mock_generate.assert_called_once()


def test_analyze_log_upstream_timeout(monkeypatch):
    mock_generate = MagicMock(side_effect=requests.exceptions.Timeout("Read timeout"))
    monkeypatch.setattr(llm_provider, "generate", mock_generate)

    response = client.post(
        "/analyze-log",
        json={"raw_log": "Any log will trigger the mocked timeout here ..."},
    )

    assert response.status_code == 504
    assert "Gateway Timeout" in response.json()["detail"]
