import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
import requests

from log_analyzer import app, llm_provider, LogAnalysis

client = TestClient(app)


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
