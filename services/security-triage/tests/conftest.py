import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
