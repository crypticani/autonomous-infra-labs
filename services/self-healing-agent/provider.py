"""The model backend, behind one interface -- including the shape of the transcript.

`generate(system, user) -> str` is not enough here. An agent turn is prose, or a request
to call functions, or both, and the transcript itself is provider-shaped: Gemini requires
the model's own turn echoed back verbatim, so it cannot be rebuilt from a list of
ToolCalls without losing part ordering and thought signatures.

So this interface owns three things: how to phrase a user message, how to take a turn,
and how to phrase a tool result. agent.py never constructs a message, which is what makes
the loop provider-agnostic rather than Gemini-shaped with an adapter bolted on.

**Automatic function calling is disabled on every request**, and tools are declared as
schemas rather than callables. With AFC on -- the SDK's default -- google-genai executes
tool functions itself, which here means calling restart_pod with no human in the path.
Two defences: the flag, and never handing over a callable to invoke.
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

# A diagnosis is six to ten chained calls held only in memory, so losing the ninth to a
# 503 discards the eight that worked. Only failures that could succeed unchanged are
# retried: a 400 is a request this code built wrong, a 404 is a wrong model name.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = int(os.getenv("SHA_MODEL_RETRIES", "3"))
# Doubles per attempt: 1s, 2s. Bounded at 3s added latency.
RETRY_BACKOFF = float(os.getenv("SHA_MODEL_RETRY_BACKOFF", "1.0"))

# The retry above cannot ride this out: a live 429 named the quota (value 5 per minute)
# and asked for a 51s wait, and seven iterations fire in eight seconds. Pacing stays under
# the limit rather than recovering from a violation of it.
RATE_LIMIT = int(os.getenv("SHA_MODEL_RATE_LIMIT", "5"))
RATE_LIMIT_WINDOW = 60.0


@dataclass(frozen=True)
class ToolCall:
    """One function the model asked for. It has not run."""

    name: str
    args: dict[str, Any]
    id: str | None = None


@dataclass(frozen=True)
class AgentTurn:
    """What the model did with one turn."""

    text: str
    tool_calls: tuple[ToolCall, ...]
    raw: Any


class BaseAgentProvider(ABC):
    name: str
    model_name: str

    # Cumulative across every chat() turn: a diagnosis is four to six chained turns, so
    # "what did that cost" is a delta around the whole loop. Plain ints so `+=` rebinds
    # per instance rather than sharing a mutable class default.
    prompt_tokens = 0
    output_tokens = 0

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

        `allowed` is a hint, not a control -- it narrows what the model is *asked* to pick from
        where the backend supports it. agent.py checks every returned name against its own
        allowlist regardless, which is the check that holds.
        """


class GeminiProvider(BaseAgentProvider):
    name = "gemini"

    def __init__(self) -> None:
        if not os.getenv("GEMINI_API_KEY"):
            logger.error("GEMINI_API_KEY is not set in the environment")
        # Its own key: a distinct model name is also a distinct free-tier quota bucket.
        self.model_name = os.getenv("SHA_GEMINI_MODEL", "gemini-3.6-flash")
        self.client = genai.Client()
        # Kept for the life of the process: the per-minute cap does not reset between
        # diagnoses, so pacing has to see the earlier ones.
        self._call_times: list[float] = []
        logger.info(f"GeminiProvider using {self.model_name}")

    def _pace(self) -> None:
        """Waits, if needed, to stay under RATE_LIMIT per RATE_LIMIT_WINDOW.

        Every attempt counts, including retried ones -- a retry is a real request against the
        same quota, and undercounting is how a diagnosis lands back on a 429.
        """
        now = time.monotonic()
        self._call_times = [t for t in self._call_times if now - t < RATE_LIMIT_WINDOW]
        if len(self._call_times) >= RATE_LIMIT:
            wait = RATE_LIMIT_WINDOW - (now - self._call_times[0])
            if wait > 0:
                logger.info(
                    f"pacing to stay under {RATE_LIMIT}/{RATE_LIMIT_WINDOW:.0f}s: "
                    f"sleeping {wait:.1f}s"
                )
                time.sleep(wait)
                now = time.monotonic()
        self._call_times.append(now)

    def user(self, text: str) -> types.Content:
        return types.Content(role="user", parts=[types.Part(text=text)])

    def tool_result(self, call: ToolCall, result: dict) -> types.Content:
        # role="user", not "tool": this API has no tool role, and "user" is what the SDK's
        # own automatic-calling loop uses for function responses.
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
            # Diagnosis is not a creative task; a reproducible transcript is worth more than variety.
            "temperature": 0.0,
            "tools": [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=tool["name"],
                            description=tool["description"],
                            # Not `parameters`: the two are mutually exclusive, and
                            # tools/ already writes the JSON Schema form.
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
            # VALIDATED, not ANY: ANY forces a call every turn, so the model could never say it
            # is stuck -- the one thing this loop must be able to report.
            config["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="VALIDATED", allowed_function_names=list(allowed)
                )
            )

        # Invisible to guardrails.check_llm_call(): a 503 was never served, so charging it
        # would let an outage spend the day's budget producing no diagnosis.
        for attempt in range(1, MAX_RETRIES + 1):
            self._pace()
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

        # Only the attempt that came back: a retried 503 was never billed either.
        if usage := response.usage_metadata:
            prompt_tokens = usage.prompt_token_count or 0
            # Thinking tokens bill as output, and a tool-selection turn writes almost no prose --
            # nearly all of its cost is reasoning candidates_token_count misses.
            output_tokens = (usage.candidates_token_count or 0) + (
                usage.thoughts_token_count or 0
            )
            self.prompt_tokens += prompt_tokens
            self.output_tokens += output_tokens
            # Attributes for a caller measuring one diagnosis by delta, the counter for Grafana
            # across all of them.
            metrics.MODEL_TOKENS.labels(direction="prompt").inc(prompt_tokens)
            metrics.MODEL_TOKENS.labels(direction="output").inc(output_tokens)

        calls = tuple(
            ToolCall(name=call.name, args=call.args or {}, id=call.id)
            for call in (response.function_calls or [])
        )

        # Empty when a safety filter blocked the response -- a real branch.
        raw = response.candidates[0].content if response.candidates else None
        if raw is None:
            raise AgentProviderError(
                "the model returned no content", 502, provider=self.name
            )

        # Not response.text: it warns on non-text parts, which here is every turn that calls
        # a tool. `thought` parts are reasoning, not the answer.
        parts = raw.parts or []
        text = "".join(part.text for part in parts if part.text and not part.thought)

        return AgentTurn(text=text.strip(), tool_calls=calls, raw=raw)


@lru_cache(maxsize=1)
def get_agent_provider() -> BaseAgentProvider:
    # Gemini by default, unlike the others: four to six chained turns at 165-204s each on
    # CPU Ollama is twenty minutes for one answer.
    provider_type = os.getenv("SHA_LLM_PROVIDER", "gemini").lower()
    if provider_type == "gemini":
        return GeminiProvider()
    raise ValueError(f"Unsupported SHA_LLM_PROVIDER: {provider_type!r}")
