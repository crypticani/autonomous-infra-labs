"""The model backend, behind one interface -- including the shape of the transcript.

log-analyzer and knowledge-copilot both put `generate(system, user) -> str` behind a provider
ABC, and that is not enough here. An agent turn is not a string: it is prose, or a request to
call functions, or both. And the transcript itself is provider-shaped -- Gemini requires the
model's own turn to be echoed back into `contents` verbatim, so it cannot be rebuilt from a
list of ToolCalls without losing part ordering and thought signatures.

So this interface owns three things instead of one: how to phrase a user message, how to take
a turn, and how to phrase a tool result. agent.py never constructs a message. That is what
lets the loop be genuinely provider-agnostic, rather than Gemini-shaped with an adapter
bolted on for everything else.

Automatic function calling is disabled on every request, and tools are declared as schemas
rather than as callables. With AFC enabled -- which is the SDK's default -- google-genai
executes tool functions itself, up to ten per request. For this service that means calling
restart_pod with no human anywhere in the path. Day 18's approval gate is worth nothing if
the SDK can route around it, so there are two defences: the flag, and the fact that no
callable is ever handed over for it to invoke.
"""

import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import metrics
from errors import AgentProviderError

load_dotenv()

logger = logging.getLogger(__name__)

# Retried in place, because a diagnosis is not one model call -- it is six to ten, and the
# transcript is only in memory. Losing the ninth to a 503 discards the eight that worked and
# the tool results they cost. Day 20 is when that stops being survivable: Alertmanager calls
# this with nobody watching, so a discarded diagnosis is an alert that silently gets none.
#
# Only failures where the same request could plausibly succeed unchanged. A 400 means this
# code built a bad request; sending it twice more is three identical failures instead of one.
# 404 is a wrong model name. Neither improves by waiting.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = int(os.getenv("SHA_MODEL_RETRIES", "3"))
# Doubles per attempt: 1s, 2s. Constant backoff against a rate limit is just three requests
# into the same closed door. Total added latency is bounded at 3s, which is nothing next to
# a diagnosis that already runs for minutes.
RETRY_BACKOFF = float(os.getenv("SHA_MODEL_RETRY_BACKOFF", "1.0"))


@dataclass(frozen=True)
class ToolCall:
    """One function the model asked for. It has not run.

    `id` is Gemini's optional correlation id. Nothing dispatches on it -- the loop keys on
    `name` -- but it is passed back untouched when present, because the API may start
    requiring it for parallel calls.
    """

    name: str
    args: dict[str, Any]
    id: str | None = None


@dataclass(frozen=True)
class AgentTurn:
    """What the model did with one turn.

    `text` and `tool_calls` are not exclusive: a model may narrate and then call. Either may
    be empty. Both empty means the model said nothing at all, which the loop has to treat as
    a dead end rather than as an empty diagnosis.

    `raw` is this provider's own object for the turn, kept so the loop can append it to the
    transcript verbatim -- see the module docstring for why rebuilding it is not an option.
    The loop must never read inside it.
    """

    text: str
    tool_calls: tuple[ToolCall, ...]
    raw: Any


class BaseAgentProvider(ABC):
    name: str
    model_name: str

    @abstractmethod
    def user(self, text: str) -> Any:
        """A user message, in this provider's transcript format."""

    @abstractmethod
    def tool_result(self, call: ToolCall, result: dict) -> Any:
        """A tool's return value, in this provider's transcript format."""

    @abstractmethod
    def chat(
        self,
        system: str,
        contents: list[Any],
        tools: list[dict],
        allowed: list[str] | None = None,
    ) -> AgentTurn:
        """One turn. `tools` is a list of `{name, description, schema}` dicts.

        `allowed` is a hint, not a control. It narrows what the model is *asked* to choose
        from, where the backend supports that; Ollama's /api/chat has no equivalent
        parameter. agent.py checks every returned name against its own allowlist regardless,
        which is the check that actually holds.
        """


class GeminiProvider(BaseAgentProvider):
    name = "gemini"

    def __init__(self) -> None:
        if not os.getenv("GEMINI_API_KEY"):
            logger.error("GEMINI_API_KEY is not set in the environment")
        # Its own key rather than the copilot's GEMINI_MODEL: this service may want a
        # heavier model for reasoning about a cluster than the copilot wants for reading
        # four chunks of markdown, and one shared key makes that a coupled decision.
        self.model_name = os.getenv("SHA_GEMINI_MODEL", "gemini-3.6-flash")
        self.client = genai.Client()
        logger.info(f"GeminiProvider using {self.model_name}")

    def user(self, text: str) -> types.Content:
        return types.Content(role="user", parts=[types.Part(text=text)])

    def tool_result(self, call: ToolCall, result: dict) -> types.Content:
        # role="user", not "tool": this API has no tool role, and role="user" is what the
        # SDK's own automatic-calling loop uses for function responses. The convention for
        # the dict is {"output": ...} on success and {"error": ...} on failure -- tools/
        # owns which, not this module.
        return types.Content(
            role="user",
            parts=[types.Part.from_function_response(name=call.name, response=result)],
        )

    def chat(
        self,
        system: str,
        contents: list[Any],
        tools: list[dict],
        allowed: list[str] | None = None,
    ) -> AgentTurn:
        config: dict[str, Any] = {
            "system_instruction": system,
            # Diagnosis is not a creative task, and a reproducible transcript is worth more
            # than variety when the eval harness lands on day 21.
            "temperature": 0.0,
            "tools": [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=tool["name"],
                            description=tool["description"],
                            # parameters_json_schema, not parameters: the two are mutually
                            # exclusive, and the JSON Schema form is what tools/ writes
                            # anyway for its own argument validation.
                            parameters_json_schema=tool["schema"],
                        )
                        for tool in tools
                    ]
                )
            ],
            # The reason this class exists rather than a two-line call. See the docstring.
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        }
        if allowed:
            # VALIDATED, not ANY. ANY forces a function call on every turn, so the model
            # could never reply in prose -- including never being able to say it is stuck,
            # which is the one thing a diagnosis loop must be able to report.
            config["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="VALIDATED", allowed_function_names=list(allowed)
                )
            )

        # Retries are deliberately invisible to guardrails.check_llm_call(), which counts
        # turns the model actually took. A 503 was never served, so charging it to the
        # budget would let an outage spend the day's calls without producing a diagnosis.
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.client.models.generate_content(
                    model=self.model_name, contents=contents, config=config
                )
                break
            except genai_errors.APIError as e:
                if e.code not in RETRY_STATUSES or attempt == MAX_RETRIES:
                    raise AgentProviderError(
                        f"the model API returned an error: {e}", 502, provider=self.name
                    ) from e
                metrics.MODEL_RETRIES.labels(status=str(e.code)).inc()
                delay = RETRY_BACKOFF * 2 ** (attempt - 1)
                logger.warning(
                    f"model returned {e.code}, retrying in {delay}s "
                    f"(attempt {attempt}/{MAX_RETRIES})"
                )
                time.sleep(delay)

        calls = tuple(
            ToolCall(name=call.name, args=call.args or {}, id=call.id)
            for call in (response.function_calls or [])
        )

        # candidates is empty when a safety filter blocked the response, so this is a real
        # branch and not defensive noise.
        raw = response.candidates[0].content if response.candidates else None
        if raw is None:
            raise AgentProviderError(
                "the model returned no content", 502, provider=self.name
            )

        # Not response.text. That accessor logs a warning whenever a response contains
        # non-text parts -- which for this service is every turn that calls a tool -- and it
        # is the normal case here, not something to warn about. Reading the parts directly
        # says what we mean and stays quiet. `thought` parts are the model's reasoning, not
        # its answer.
        parts = raw.parts or []
        text = "".join(part.text for part in parts if part.text and not part.thought)

        return AgentTurn(text=text.strip(), tool_calls=calls, raw=raw)


@lru_cache(maxsize=1)
def get_agent_provider() -> BaseAgentProvider:
    # Defaults to gemini, unlike the other two services, which default to ollama. One
    # diagnosis is four to six chained turns, and generation against a CPU-only Ollama
    # measured 165-204s per turn in week 2 -- twenty minutes for one answer.
    provider_type = os.getenv("SHA_LLM_PROVIDER", "gemini").lower()
    if provider_type == "gemini":
        return GeminiProvider()
    raise ValueError(f"Unsupported SHA_LLM_PROVIDER: {provider_type!r}")
