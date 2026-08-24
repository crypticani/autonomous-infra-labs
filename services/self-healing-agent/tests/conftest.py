import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Before provider is imported: genai.Client() reads the environment at construction and
# raises without a key. No test reaches the backend, but GeminiProvider() is instantiated
# to test its translation logic, and that runs the constructor.
os.environ.setdefault("GEMINI_API_KEY", "test-key-never-sent")

import alerts  # noqa: E402
import audit  # noqa: E402
import guardrails  # noqa: E402
from prometheus_client import REGISTRY  # noqa: E402
from provider import AgentTurn, ToolCall  # noqa: E402


def metric(name: str, **labels) -> float:
    """One counter's current value, read the way Prometheus reads it -- through the registry,
    because `counter._value` would still answer for a metric whose labels are wrong.
    Counters accumulate for the process, so every caller measures a delta.
    """
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def audit_log(tmp_path, monkeypatch):
    """Reads back what landed on disk rather than what a mock was told. audit.record fsyncs, so
    the line is readable by the time a call returns -- the property the fail-before-acting
    test depends on.
    """
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", str(path))

    def events() -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    return events


@pytest.fixture(autouse=True)
def fresh_llm_budget():
    """guardrails._llm_calls is module state and diagnose() appends every turn. Without this,
    the thirty-first turn in the suite fails a guardrail instead of the loop under test.
    """
    guardrails._llm_calls.clear()
    yield
    guardrails._llm_calls.clear()


@pytest.fixture(autouse=True)
def fresh_alert_dedup():
    """Same hazard one module over: alerts._seen is process state, so the second test to send
    a fingerprint would be deduplicated by the first.
    """
    alerts._seen.clear()
    yield
    alerts._seen.clear()


class FakeAgentProvider:
    """A scripted model: returns the turns it was handed, in order, and counts its calls."""

    name = "fake"
    model_name = "fake-model"

    def __init__(self, turns: list[AgentTurn]) -> None:
        self.turns = list(turns)
        self.calls = 0
        self.seen_allowed: list[list[str] | None] = []
        self.seen_contents: list[list] = []

    def user(self, text: str) -> dict:
        return {"role": "user", "text": text}

    def tool_result(self, call: ToolCall, result: dict) -> dict:
        return {"role": "tool", "name": call.name, "result": result}

    def chat(self, system, contents, tools, allowed=None) -> AgentTurn:
        self.calls += 1
        self.seen_allowed.append(list(allowed) if allowed else None)
        self.seen_contents.append(list(contents))
        if not self.turns:
            # Louder than a default turn: a loop that asked for more turns than the test
            # scripted did not terminate when it should have.
            raise AssertionError(
                f"FakeAgentProvider ran out of scripted turns after {self.calls} calls"
            )
        return self.turns.pop(0)


def turn(text: str = "", calls: tuple = ()) -> AgentTurn:
    """An AgentTurn without the ceremony. `raw` is a marker dict: the loop only ever echoes it
    back and never reads inside, and being unreadable is how a test proves that.
    """
    return AgentTurn(
        text=text,
        tool_calls=tuple(calls),
        raw={"role": "model", "echo": text, "asked": [c.name for c in calls]},
    )


def call(name: str, **args) -> ToolCall:
    return ToolCall(name=name, args=args)


@pytest.fixture
def fake_provider():
    def _make(*turns: AgentTurn) -> FakeAgentProvider:
        return FakeAgentProvider(list(turns))

    return _make
