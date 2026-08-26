"""The model backend for triage -- Ollama or Gemini, behind one seam.

One batch in, one validated JSON object out; no multi-turn shape to carry, unlike the
agent's provider. `schema` is a Pydantic class, not a dict, because Gemini wants the
class and Ollama wants `.model_json_schema()` -- passing the class lets each ask for
what it needs and keeps this module ignorant of what TriageBatch contains.

No retry or rate-limit pacing, unlike the agent's: a triage batch is one call and the
default backend has no quota ceiling.
"""

import logging
import os
from abc import ABC, abstractmethod
from functools import lru_cache

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel

import metrics
from errors import TriageProviderError

load_dotenv()

logger = logging.getLogger(__name__)

# Its own variable, not the copilot's shared LLM_TIMEOUT: a copilot answer is one call a
# human waits on, a triage batch is one of many in a background run.
#
# 600 because 300 was not enough -- the same five findings measured 158.7s, 208.4s, then
# over 300s. Output tokens set the wall clock at ~2.5-3 tok/s and vary with the *answer*,
# not just the input. A timeout wastes the whole attempt; nothing here prefers that.
LLM_TIMEOUT = int(os.getenv("ST_LLM_TIMEOUT", "600"))

# Found live 2026-08-19: an identical prompt to a clean ~750-token run instead generated
# 3,613+ and never stopped. Greedy decoding with no repetition penalty has no way out of
# a repeating attractor, and num_predict unset bounds nothing. ~2x one batch's need.
MAX_TOKENS = int(os.getenv("ST_MAX_TOKENS", "1536"))


class BaseTriageProvider(ABC):
    name: str
    model_name: str

    # Cumulative across every generate(). Attributes rather than a second return value,
    # so triage.py keeps working without knowing tokens exist; cumulative so a caller
    # making several calls reads one delta instead of summing. Plain ints, because
    # `+= n` rebinds on the instance where a mutable default would be shared.
    #
    # ponytail: unsynchronised on an lru_cache'd singleton, so concurrent runs interleave
    # their increments. Totals stay right, a straddling delta does not; bench.py is
    # sequential and is the only delta reader.
    prompt_tokens = 0
    output_tokens = 0

    def _count(self, prompt: int, output: int) -> None:
        """Record one call's usage in both places it has to land."""
        self.prompt_tokens += prompt
        self.output_tokens += output
        metrics.MODEL_TOKENS.labels(direction="prompt").inc(prompt)
        metrics.MODEL_TOKENS.labels(direction="output").inc(output)

    @abstractmethod
    def generate(self, system: str, user: str, schema: type[BaseModel]) -> str:
        """Raw JSON text constrained to `schema`. Caller parses and validates it --
        this seam only knows how to reach a model, not what triage means."""


class OllamaProvider(BaseTriageProvider):
    name = "ollama"

    def __init__(self) -> None:
        # 7b is load-bearing, not a default nobody measured: 1.5b returned five
        # byte-identical explanations and needs_human for everything, every guard
        # satisfied. 7b is right at ~3.3x the wall clock.
        self.model_name = os.getenv("ST_OLLAMA_MODEL", "qwen2.5-coder:7b")
        # Service-specific first: the deploy runs all five services off one shared .env
        # and only this one's backend moved to a laptop, so editing the shared name would
        # silently take the other two with it. The fallback means a host where everything
        # does share a backend needs no new variable.
        self.base_url = os.getenv("ST_OLLAMA_BASE_URL") or os.getenv(
            "OLLAMA_BASE_URL", "http://localhost:11434"
        )
        logger.info(f"OllamaProvider using {self.model_name} at {self.base_url}")

    def generate(self, system: str, user: str, schema: type[BaseModel]) -> str:
        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model_name,
                    "system": system,
                    "prompt": user,
                    "format": schema.model_json_schema(),
                    "options": {
                        # Removes sampling noise, not batch-dependent variance: prompt
                        # cache reuse can still flip a near-tie logit.
                        "temperature": 0.0,
                        # Backstop: a truncated response fails validation, which is a
                        # 502 in seconds rather than a hang that looks like slowness.
                        "num_predict": MAX_TOKENS,
                        # 1.05 measured too mild on 2026-08-19; 1.3 is the belt to
                        # explanation's max_length suspenders.
                        "repeat_penalty": 1.3,
                    },
                    "stream": False,
                },
                timeout=LLM_TIMEOUT,
            )
            response.raise_for_status()
        except requests.exceptions.Timeout as e:
            raise TriageProviderError(
                "the model took too long to answer", 504, provider=self.name
            ) from e
        except requests.exceptions.HTTPError as e:
            raise TriageProviderError(
                f"the model backend rejected the request: {e}",
                502,
                provider=self.name,
            ) from e
        except requests.exceptions.RequestException as e:
            raise TriageProviderError(
                f"the model backend is unreachable: {e}", 503, provider=self.name
            ) from e

        body = response.json()
        # prompt_eval_count is the system prompt plus the batch -- charged once per call
        # however many findings ride along, which is what makes batching pay.
        self._count(body.get("prompt_eval_count") or 0, body.get("eval_count") or 0)

        answer = (body.get("response") or "").strip()
        if not answer:
            raise TriageProviderError(
                "the model returned an empty response", 502, provider=self.name
            )
        return answer


class GeminiProvider(BaseTriageProvider):
    name = "gemini"

    def __init__(self) -> None:
        if not os.getenv("GEMINI_API_KEY"):
            logger.error("GEMINI_API_KEY is not set in the environment")
        # Its own variable, and its own model *name*: the free-tier quota is scoped
        # per-project-per-model, and two services sharing a name has already cost a
        # diagnosis. Not rolled out to the others for that reason.
        self.model_name = os.getenv("ST_GEMINI_MODEL", "gemini-3.7-flash")
        self.client = genai.Client()
        logger.info(f"GeminiProvider using {self.model_name}")

    def generate(self, system: str, user: str, schema: type[BaseModel]) -> str:
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=user,
                config={
                    "system_instruction": system,
                    "temperature": 0.0,
                    "response_mime_type": "application/json",
                    "response_schema": schema,
                },
            )
        except genai_errors.APIError as e:
            raise TriageProviderError(
                f"the model API returned an error: {e}", 502, provider=self.name
            ) from e

        # None when the request never reached billing -- a safety block, say. A real
        # branch, not defensive noise.
        if usage := response.usage_metadata:
            # Reasoning tokens are billed at the output rate: measured, "reply with the
            # single word ok" spent 119 thinking tokens for 1 candidate token.
            self._count(
                usage.prompt_token_count or 0,
                (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
            )

        answer = (response.text or "").strip()
        if not answer:
            raise TriageProviderError(
                "the model returned an empty response", 502, provider=self.name
            )
        return answer


@lru_cache(maxsize=1)
def get_triage_provider() -> BaseTriageProvider:
    # Ollama, unlike the agent's default: no quota ceiling, and scan data never leaves
    # the tailnet.
    provider_type = os.getenv("ST_LLM_PROVIDER", "ollama").lower()
    if provider_type == "ollama":
        return OllamaProvider()
    if provider_type == "gemini":
        return GeminiProvider()
    raise ValueError(f"Unsupported ST_LLM_PROVIDER: {provider_type!r}")
