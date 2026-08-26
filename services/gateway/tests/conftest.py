import os
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Assigned, not setdefault'ed, and before any import below. `load_dotenv()` runs at import
# in three modules here and five services share one root `.env`, so without this the suite
# reads real tokens locally and none on a runner -- and both versions pass sometimes.
os.environ["GEMINI_API_KEY"] = "test-key-never-sent"
os.environ["GW_API_TOKENS"] = ""
os.environ["GW_ROUTE_ON"] = "medium"
os.environ["GW_LLM_PROVIDER"] = "ollama"

# /health calls /api/tags, so an unpinned address means the suite reaches a real Ollama.
# `.invalid` never resolves, so a test that forgets to stub it fails loudly.
os.environ["GW_OLLAMA_BASE_URL"] = "http://ollama.invalid:11434"
os.environ["GW_OLLAMA_MODEL"] = "test-router-model"

# Backend addresses and the tokens the gateway presents to them.
os.environ["GW_LOG_ANALYZER_URL"] = "http://log-analyzer.test:7000"
os.environ["GW_KNOWLEDGE_COPILOT_URL"] = "http://knowledge-copilot.test:7100"
os.environ["GW_SELF_HEALING_AGENT_URL"] = "http://self-healing-agent.test:7200"
os.environ["GW_SECURITY_TRIAGE_URL"] = "http://security-triage.test:7300"
os.environ["KC_API_TOKEN"] = "kc-test-token"
os.environ["SHA_API_TOKEN"] = "sha-test-token"
os.environ["ST_API_TOKENS"] = "st-test-token,st-second-token"

from prometheus_client import REGISTRY  # noqa: E402


def metric(name: str, **labels) -> float:
    """One counter's current value, read the way Prometheus reads it."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


class FakeResponse:
    """Enough of requests.Response to forward or refuse. Not a Mock, because `_safe_json`
    depends on `.json()` raising ValueError and two tests are about that."""

    def __init__(self, status_code: int = 200, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        """/health's /api/tags call uses it."""
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


@pytest.fixture
def route_json():
    """The router's own output, as the raw JSON string a provider returns."""

    def build(service="knowledge-copilot", confidence=0.9, reason="a runbook question"):
        import json

        return json.dumps(
            {"reason": reason, "service": service, "confidence": confidence}
        )

    return build
