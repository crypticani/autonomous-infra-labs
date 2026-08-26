import os
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Every one of these is assigned, not setdefault'ed, and that is the whole point of the
# block. `load_dotenv()` runs at import in app.py, provider.py and backends.py, and the
# five services share one root `.env` -- so on this laptop the suite would otherwise read
# real tokens and real backend addresses, and on a CI runner it would read none. Both
# versions pass sometimes. Day 28 lost a whole workflow to exactly this with KC_API_TOKEN,
# and load_dotenv's override=False means an explicit assignment here wins on both.
#
# Assigned before any module below is imported, because the constants they set are read at
# import time.
os.environ["GEMINI_API_KEY"] = "test-key-never-sent"
os.environ["GW_API_TOKENS"] = ""
os.environ["GW_ROUTE_ON"] = "medium"
os.environ["GW_LLM_PROVIDER"] = "ollama"

# Both pinned for a sharper reason than the rest: /health now calls the model backend's
# /api/tags, so an unpinned address is a suite that reaches a real Ollama over the tailnet
# -- passing here, hanging on a runner, and passing again on any laptop where the tailnet
# happens to be up. `.invalid` is RFC 2606 reserved and never resolves, so a test that
# forgets to stub the call fails loudly instead of quietly going out to the network.
os.environ["GW_OLLAMA_BASE_URL"] = "http://ollama.invalid:11434"
os.environ["GW_OLLAMA_MODEL"] = "test-router-model"

# Backend addresses and the tokens the gateway presents to them. Read per call rather than
# at import, so these are here to stop the real deploy's values leaking in rather than to
# be loaded once.
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
    """Enough of requests.Response for the gateway to forward or refuse it.

    Its own class rather than a Mock: `_safe_json` depends on `.json()` raising ValueError
    on a non-JSON body, and that behaviour is what two of these tests are about.
    """

    def __init__(self, status_code: int = 200, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        """/health's /api/tags call uses it, and a stub that silently lacked it made eight
        tests fail with an AttributeError rather than an assertion."""
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
