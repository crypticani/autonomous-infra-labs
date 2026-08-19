"""The model backend for triage -- Ollama or Gemini, behind one seam.

Unlike self-healing-agent/provider.py, a triage call is not a multi-turn agent loop --
one batch of findings in, one validated JSON object out -- so there's no AgentTurn or
ToolCall shape to carry here. What *is* worth keeping from that module is the rest of the
seam: an ABC, a provider-specific error carrying an HTTP status, and a factory switched
by an env var, so Day 27's cost/latency benchmark can flip providers with nothing but
ST_LLM_PROVIDER.

`schema` is a Pydantic model class, not a dict: Gemini's `response_schema` wants the
class itself, Ollama's `format` wants `.model_json_schema()`. Passing the class once
lets each provider ask it for whichever shape it needs, and keeps this module ignorant
of what TriageBatch actually contains -- triage.py owns that.

No retry or rate-limit pacing here, unlike self-healing-agent/provider.py: that module
earned both from a live incident (a diagnosis losing its 9th of ten chained calls to a
transient 503) and Gemini's free-tier per-minute cap. A triage batch is one call, and the
default provider (Ollama) has no quota ceiling to pace against. Add retry if Day 27's
benchmark shows CPU Ollama failing transiently often enough to be worth it.
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

from errors import TriageProviderError

load_dotenv()

logger = logging.getLogger(__name__)

# Shared with knowledge-copilot's LLM_TIMEOUT: same physical CPU-Ollama host on appsrv,
# same reason 120s wasn't enough there (195s measured for one grounded answer). Day 23's
# own verify step measures whether a batch needs longer than that.
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "300"))

# Found live on 2026-08-19: an identical prompt to a clean 47s/~750-token run instead
# generated 3,613+ tokens and never stopped, filling the 4096 context. Greedy decoding
# (temperature 0) with no repetition penalty has no way out of a repeating attractor once
# it enters one, and with num_predict unset (-1) nothing else bounds it -- what looked
# like appsrv's 2 cores being slow was actually this, unbounded, on any hardware. Sized
# at ~2x the tokens one full ST_BATCH_SIZE=5 batch needed; revisit if that default changes.
MAX_TOKENS = int(os.getenv("ST_MAX_TOKENS", "1536"))


class BaseTriageProvider(ABC):
    name: str
    model_name: str

    @abstractmethod
    def generate(self, system: str, user: str, schema: type[BaseModel]) -> str:
        """Raw JSON text constrained to `schema`. Caller parses and validates it --
        this seam only knows how to reach a model, not what triage means."""


class OllamaProvider(BaseTriageProvider):
    name = "ollama"

    def __init__(self) -> None:
        # qwen2.5-coder:1.5b, not knowledge-copilot's 7b-instruct: the plan's own risk
        # section calls for starting small on CPU and letting Day 27 measure whether a
        # bigger model is worth the latency.
        self.model_name = os.getenv("ST_OLLAMA_MODEL", "qwen2.5-coder:1.5b")
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
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
                        # Triage is a judgment, not a creative task -- a batch re-run on
                        # an unchanged prompt should score the same way every time. Not a
                        # guarantee, though: prompt-cache reuse changes batch splits and
                        # can still flip a near-tie logit, so temp 0 removes sampling
                        # noise, not batch-dependent variance.
                        "temperature": 0.0,
                        # The hard backstop: a truncated response still fails
                        # TriageBatch.model_validate_json, which is a real 502 in
                        # seconds instead of a hang that looks like slowness.
                        "num_predict": MAX_TOKENS,
                        # Reduces how often generation enters a repeating loop at all --
                        # greedy decoding (temperature 0) plus the default 1.0 has no
                        # escape once it does. 1.05 measured live on 2026-08-19 as too
                        # mild: qwen2.5-coder:1.5b still fell into a repeating
                        # conditional dozens of times over before num_predict cut it
                        # off. 1.3 is the belt to explanation's max_length suspenders.
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

        answer = (response.json().get("response") or "").strip()
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
        # Its own variable, not GEMINI_MODEL or SHA_GEMINI_MODEL: Gemini's free-tier
        # quota is scoped per-project-per-model, and Day 21 already lost a diagnosis to
        # two services silently sharing one model name's bucket.
        self.model_name = os.getenv("ST_GEMINI_MODEL", "gemini-3.6-flash")
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

        answer = (response.text or "").strip()
        if not answer:
            raise TriageProviderError(
                "the model returned an empty response", 502, provider=self.name
            )
        return answer


@lru_cache(maxsize=1)
def get_triage_provider() -> BaseTriageProvider:
    # Ollama, unlike self-healing-agent's default: no quota ceiling, and scan data
    # (someone else's repo layout and dependency versions) never leaves the tailnet.
    provider_type = os.getenv("ST_LLM_PROVIDER", "ollama").lower()
    if provider_type == "ollama":
        return OllamaProvider()
    if provider_type == "gemini":
        return GeminiProvider()
    raise ValueError(f"Unsupported ST_LLM_PROVIDER: {provider_type!r}")
