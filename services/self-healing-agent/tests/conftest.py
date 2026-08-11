import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Before provider is imported: genai.Client() reads the environment at construction and
# raises without a key. No test reaches the real backend, but GeminiProvider() is
# instantiated to test its translation logic, and that runs the constructor.
os.environ.setdefault("GEMINI_API_KEY", "test-key-never-sent")

from provider import AgentTurn, ToolCall  # noqa: E402


class FakeAgentProvider:
    """A scripted model: returns the turns it was handed, in order, and counts its calls.

    This is what lets every later day specify a model's *behaviour* -- "asks for logs, then
    submits a diagnosis" -- with no network and no key. knowledge-copilot's SpyLLM does the
    same job for a single generate() call; an agent needs a sequence, and needs to record
    what it was asked so a test can assert the loop narrowed the allowlist.

    It is also the second implementation of BaseAgentProvider, which is the only real proof
    that the interface is not Gemini's shape wearing an abstract base class.
    """

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
            # Louder than returning a default turn: a loop that asked for more turns than
            # the test scripted is a loop that did not terminate when it should have, and
            # that is the failure worth seeing by name.
            raise AssertionError(
                f"FakeAgentProvider ran out of scripted turns after {self.calls} calls"
            )
        return self.turns.pop(0)


def turn(text: str = "", calls: tuple = ()) -> AgentTurn:
    """An AgentTurn without the ceremony.

    `raw` is a marker dict rather than anything meaningful: the loop's contract is that it
    only ever echoes raw back, never reads inside it, and a marker is how a test can prove
    that by being unreadable in any useful way.
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
