import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Before any test module imports provider, and this line is why Security Triage CI was
# red on every run from Day 25 to Day 28. `genai.Client()` reads the environment at
# construction and raises without a key; no test here reaches the real backend, but
# GeminiProvider() is instantiated to test its translation logic and that runs the
# constructor. Locally it passed because provider.py calls load_dotenv() and the repo's
# .env has a key -- on a runner there is no .env, so six tests failed and the whole
# workflow with them. Copied from self-healing-agent/tests/conftest.py, which has carried
# this since Day 15; this service had no conftest.py at all until Day 28, which is the
# entire reason it never inherited the fix.
#
# setdefault, and load_dotenv's default of override=False, mean the placeholder also wins
# locally -- so the suite never touches a real key on any machine.
os.environ.setdefault("GEMINI_API_KEY", "test-key-never-sent")

import metrics  # noqa: E402
from prometheus_client import REGISTRY  # noqa: E402


def metric(name: str, **labels) -> float:
    """One counter's current value, read the way Prometheus reads it.

    Through the registry rather than `counter._value`, because the private attribute
    would still answer for a metric whose labels are wrong -- and a mislabelled counter
    is exactly the failure that survives review and then shows up as an empty graph.
    Same helper as self-healing-agent/tests/conftest.py.

    Counters accumulate for the life of the process, so every caller measures a delta
    across the thing under test rather than an absolute.
    """
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture(autouse=True)
def fresh_repo_labels():
    """metrics._repos is module state that only ever grows, so without this the test
    that fills it to MAX_REPO_LABELS would leave every later test's repo folded into
    `other` -- test order deciding test outcome, in a file nobody suspects.
    """
    metrics._repos.clear()
    yield
    metrics._repos.clear()
