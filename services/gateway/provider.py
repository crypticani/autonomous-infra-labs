"""The model backend for the intent router -- Ollama or Gemini, behind one seam.

The fifth copy of this shape in the repo, and deliberately the thinnest. A routing decision
is one call, needs no multi-turn transcript, and has nothing to retry into: if the model
cannot say which service a question belongs to, saying so is the correct answer rather than
something to try again for.

The default is Ollama, like security-triage's and unlike the agent's -- no quota ceiling,
and the user asked for Gemini to be the flip rather than the default because the free tier
has already run out once.
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
from errors import GatewayProviderError

load_dotenv()

logger = logging.getLogger(__name__)

# 120, not triage's 600: this is the only model call in the repo a human waits on
# synchronously. A router that takes ten minutes is broken, not slow, and the caller has
# already given up. Its own variable for the same reason the others have theirs.
LLM_TIMEOUT = int(os.getenv("GW_LLM_TIMEOUT", "120"))

# A routing decision is a sentence and two fields. 256 is roughly 4x what one needs, and
# the ceiling exists at all because an unbounded generation loop filled the context on
# 2026-08-19 with num_predict unset.
MAX_TOKENS = int(os.getenv("GW_MAX_TOKENS", "256"))


class BaseRouterProvider(ABC):
    name: str
    model_name: str

    # Cumulative across every generate(). Attributes rather than a second return value, so
    # router.py stays ignorant of tokens; cumulative so eval_router.py reads one delta
    # instead of summing per-call numbers.
    prompt_tokens = 0
    output_tokens = 0

    def _count(self, prompt: int, output: int) -> None:
        self.prompt_tokens += prompt
        self.output_tokens += output
        metrics.MODEL_TOKENS.labels(direction="prompt").inc(prompt)
        metrics.MODEL_TOKENS.labels(direction="output").inc(output)

    @abstractmethod
    def generate(self, system: str, user: str, schema: type[BaseModel]) -> str:
        """Raw JSON text constrained to `schema`. The caller parses and validates it."""


class OllamaProvider(BaseRouterProvider):
    name = "ollama"

    def __init__(self) -> None:
        # An instruct model, not the coder model triage runs: this call classifies prose
        # about incidents, which is the thing an instruct tune is for. Both are already
        # pulled, so the choice cost nothing to make on the merits.
        #
        # 7b is the fail-safe default and eval_router.py is what may lower it. Day 23's
        # lesson was that 1.5b writes unusable triage explanations -- it never showed
        # 1.5b bad at classification, which is a much smaller job. Evidence gets to
        # downgrade this; a wish for a faster demo does not.
        self.model_name = os.getenv("GW_OLLAMA_MODEL", "qwen2.5:7b-instruct")
        # Service-specific first, then the shared name: the deploy runs five services off
        # one .env, and ST_OLLAMA_BASE_URL exists because exactly one of them needed to
        # point somewhere else. The fallback means a host where everything shares a
        # backend needs no new variable.
        self.base_url = os.getenv("GW_OLLAMA_BASE_URL") or os.getenv(
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
                        "temperature": 0.0,
                        "num_predict": MAX_TOKENS,
                        # Same 1.3 as triage. A router has less room to run away in than a
                        # batch of explanations, but `reason` is still free prose.
                        "repeat_penalty": 1.3,
                    },
                    "stream": False,
                },
                timeout=LLM_TIMEOUT,
            )
            response.raise_for_status()
        except requests.exceptions.Timeout as e:
            raise GatewayProviderError(
                "the router model took too long to answer", 504, provider=self.name
            ) from e
        except requests.exceptions.HTTPError as e:
            raise GatewayProviderError(
                f"the router model backend rejected the request: {e}",
                502,
                provider=self.name,
            ) from e
        except requests.exceptions.RequestException as e:
            raise GatewayProviderError(
                f"the router model backend is unreachable: {e}",
                503,
                provider=self.name,
            ) from e

        body = response.json()
        self._count(body.get("prompt_eval_count") or 0, body.get("eval_count") or 0)

        answer = (body.get("response") or "").strip()
        if not answer:
            raise GatewayProviderError(
                "the router model returned an empty response", 502, provider=self.name
            )
        return answer


class GeminiProvider(BaseRouterProvider):
    name = "gemini"

    def __init__(self) -> None:
        if not os.getenv("GEMINI_API_KEY"):
            logger.error("GEMINI_API_KEY is not set in the environment")
        # Its own model *name*, following SHA_ and ST_: the free-tier quota is scoped
        # per-project-per-model, so two services sharing a name drain one bucket. That
        # has already cost a diagnosis once.
        self.model_name = os.getenv("GW_GEMINI_MODEL", "gemini-3.7-flash-lite")
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
            raise GatewayProviderError(
                f"the router model API returned an error: {e}", 502, provider=self.name
            ) from e

        # None when the request never reached billing -- a safety block, say.
        if usage := response.usage_metadata:
            # Reasoning tokens bill at the output rate. Measured on Day 27: "reply with
            # the single word ok" spent 119 thinking tokens for 1 candidate token.
            self._count(
                usage.prompt_token_count or 0,
                (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
            )

        answer = (response.text or "").strip()
        if not answer:
            raise GatewayProviderError(
                "the router model returned an empty response", 502, provider=self.name
            )
        return answer


@lru_cache(maxsize=1)
def get_router_provider() -> BaseRouterProvider:
    provider_type = os.getenv("GW_LLM_PROVIDER", "ollama").lower()
    if provider_type == "ollama":
        return OllamaProvider()
    if provider_type == "gemini":
        return GeminiProvider()
    raise ValueError(f"Unsupported GW_LLM_PROVIDER: {provider_type!r}")
