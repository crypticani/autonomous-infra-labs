import logging
import os
from abc import ABC, abstractmethod
from functools import lru_cache

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors

load_dotenv()

logger = logging.getLogger(__name__)

LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "120"))


class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class BaseLLMProvider(ABC):
    name: str
    model_name: str

    @abstractmethod
    def generate(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.1
    ) -> str: ...


class OllamaProvider(BaseLLMProvider):
    name = "ollama"

    def __init__(self) -> None:
        self.model_name = os.getenv("OLLAMA_CHAT_MODEL", "qwen2.5:7b-instruct")
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        logger.info(f"OllamaProvider using {self.model_name} at {self.base_url}")

    def generate(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.1
    ) -> str:
        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model_name,
                    "system": system_prompt,
                    "prompt": user_prompt,
                    "options": {"temperature": temperature},
                    "stream": False,
                },
                timeout=LLM_TIMEOUT,
            )
            response.raise_for_status()
        except requests.exceptions.Timeout as e:
            raise UpstreamError("the model took too long to answer", 504) from e
        except requests.exceptions.HTTPError as e:
            raise UpstreamError(
                f"the model backend rejected the request: {e}", 502
            ) from e
        except requests.exceptions.RequestException as e:
            raise UpstreamError(f"the model backend is unreachable: {e}", 503) from e

        answer = (response.json().get("response") or "").strip()
        if not answer:
            raise UpstreamError("the model returned an empty response", 502)
        return answer


class GeminiProvider(BaseLLMProvider):
    name = "gemini"

    def __init__(self) -> None:
        if not os.getenv("GEMINI_API_KEY"):
            logger.error("GEMINI_API_KEY is not set in the environment")
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
        self.client = genai.Client()
        logger.info(f"GeminiProvider using {self.model_name}")

    def generate(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.1
    ) -> str:
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=user_prompt,
                config={
                    "system_instruction": system_prompt,
                    "temperature": temperature,
                },
            )
        except genai_errors.APIError as e:
            raise UpstreamError(f"the model API returned an error: {e}", 502) from e
        answer = (response.text or "").strip()
        if not answer:
            raise UpstreamError("the model returned an empty response", 502)
        return answer


@lru_cache(maxsize=1)
def get_llm_provider() -> BaseLLMProvider:
    provider_type = os.getenv("LLM_PROVIDER", "ollama").lower()
    if provider_type == "ollama":
        return OllamaProvider()
    if provider_type == "gemini":
        return GeminiProvider()
    raise ValueError(f"Unsupported LLM_PROVIDER: {provider_type!r}")
