"""The model backend for the intent router -- Ollama or Gemini, behind one seam.

One call, no retry: if the model cannot say which service a question belongs to, saying so
is the answer. Ollama by default, Gemini by env flip.
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

# 300, from a measurement rather than the principle. 120 came from "a human waits on this
# one synchronously", which is true and was never checked against a cold start: on the
# deployed host the same call measured 9.0s warm and 186.9s cold, almost all of it model
# load, so 120 guaranteed a 504 on the first call after any idle period.
LLM_TIMEOUT = int(os.getenv("GW_LLM_TIMEOUT", "300"))

# ~4x what a routing decision needs. A ceiling exists at all because an unbounded
# generation loop filled the context once.
MAX_TOKENS = int(os.getenv("GW_MAX_TOKENS", "256"))


class BaseRouterProvider(ABC):
    name: str
    model_name: str

    # Cumulative, so eval_router.py reads one delta rather than summing calls.
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
        # An instruct tune, not triage's coder model: this call classifies prose. Set
        # from eval_router.py -- 22/24 against 1.5b's 13/24. Do not lower it for speed.
        self.model_name = os.getenv("GW_OLLAMA_MODEL", "qwen2.5:7b-instruct")
        # Service-specific first, then the shared name: five services share one .env.
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
                        # `reason` is free prose, so the same 1.3 as triage.
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
        # Its own model *name*: the free-tier quota is per-project-per-model, so a shared
        # name means two services draining one bucket.
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
            # Reasoning tokens bill at the output rate, and can dwarf the candidate.
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
