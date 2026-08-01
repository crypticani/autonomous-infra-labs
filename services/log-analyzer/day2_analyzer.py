import os
import json
import logging
import requests
from abc import ABC, abstractmethod
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field, ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.json import JSON
from typing import Literal

console = Console()
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class LogAnalysis(BaseModel):
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    likely_cause: str
    suggested_fix: str
    confidence: float = Field(ge=0.0, le=1.0)


class BaseLLMProvider(ABC):
    @abstractmethod
    def generate(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.1
    ) -> LogAnalysis:
        pass


class OllamaProvider(BaseLLMProvider):
    def __init__(self):
        self.model_name = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        logger.info(f"OllamaProvider initialized with model: {self.model_name}\
                    and base URL: {self.base_url}")

    def generate(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.1
    ) -> LogAnalysis:
        url = f"{self.base_url}/api/generate"

        json_system_prompt = (
            f"{system_prompt}\n\n"
            "IMPORTANT: Return ONLY valid JSON matching the following schema."
            "Do not include markdown fences, code blocks, or commentry.\n"
            f"{json.dumps(LogAnalysis.model_json_schema())}"
        )

        payload = {
            "model": self.model_name,
            "system": json_system_prompt,
            "prompt": user_prompt,
            "temperature": temperature,
            "stream": False,
            "format": LogAnalysis.model_json_schema(),
        }

        try:
            logger.info("Sending request to Ollama API")
            response = requests.post(url, json=payload, timeout=60)
            response.raise_for_status()
            raw_text = response.json().get("response", "")

            try:
                parsed_json = json.loads(raw_text)
                return LogAnalysis.model_validate(parsed_json)
            except json.JSONDecodeError as e:
                logger.exception(f"Failed to decode JSON from Ollama. Raw response: \
                        {raw_text}")
                raise ValueError(f"Ollama returned malformed JSON: {e}")
            except ValidationError as e:
                logger.exception(
                    f"Pydantic validation failed for Ollama. Raw response: \
                        {raw_text}"
                )
                raise ValueError(f"Ollama response failed schema constraints: \
                                 {e}")

        except requests.exceptions.RequestException as e:
            logger.exception(f"Failed to generate response from Ollama API: \
                             {e}")
            raise


class GeminiProvider(BaseLLMProvider):
    def __init__(self):
        if not os.getenv("GEMINI_API_KEY"):
            logger.error("GEMINI_API_KEY is not set in the environment \
                         variables.")

        self.model_name = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
        self.client = genai.Client()
        logger.info(f"GeminiProvider initialized with model: \
                    {self.model_name}")

    def generate(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.1
    ) -> LogAnalysis:
        try:
            logger.info("Sending request to Gemini API")
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=user_prompt,
                config={
                    "system_instruction": system_prompt,
                    "temperature": temperature,
                    "response_mime_type": "application/json",
                    "response_schema": LogAnalysis,
                },
            )
            raw_text = response.text

            try:
                parsed_json = json.loads(raw_text)
                return LogAnalysis.model_validate(parsed_json)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to decode JSON from Gemini. \
                             Raw response:\n{raw_text}")
                raise ValueError(f"Gemini returned malformed JSON: {e}")
            except ValidationError as e:
                logger.error(f"Pydantic validation failed for Gemini. \
                             Raw response:\n{raw_text}")
                raise ValueError(f"Gemini response failed schema constraints: \
                                 {e}")

        except Exception as e:
            logger.critical(f"Gemini API call failed: {e}")
            raise


def analyze_log(provider: BaseLLMProvider, error_log: str) -> None:
    system_instruction = (
        "You are a Senior DevOps Engineer with expertise in analyzing error \
            logs. "
        "Analyze the following log.\n"
        "1. Identify the root cause of the failure.\n"
        "2. Explain it in concise, plain English.\n"
        "3. Suggest one concrete remediation step.]\n"
        "Do not hallucinate or invent metrics not present in the log"
    )

    logger.info("Starting log analysis task...")

    try:
        analysis: LogAnalysis = provider.generate(
            system_prompt=system_instruction,
            user_prompt=f"RAW LOG:\n{error_log}",
            temperature=0.1,
        )

        raw_json_string = analysis.model_dump_json(indent=4)

        highlighted_json = JSON(raw_json_string)

        console.print(
            Panel(
                highlighted_json,
                title="[bold cyan]AI Diagnostic Report (Structured)\
                    [/bold cyan]",
                border_style="cyan",
                expand=False,
            )
        )

        logger.info("Log analysis completed successfully.")

    except Exception as e:
        logger.critical(f"Log analysis pipeline aborted: {e}")


if __name__ == "__main__":
    sample_raw_log = """
    Name:           payment-service-pod-xyz123
    State:          Waiting
      Reason:       CrashLoopBackOff
    Last State:     Terminated
      Reason:       OOMKilled
      Exit Code:    137
      Started:      Sun, 26 Jul 2026 22:15:00 +0530
      Finished:     Sun, 26 Jul 2026 22:16:30 +0530
    Ready:          False
    Restart Count:  5
    Limits:
      cpu:     500m
      memory:  256Mi
    Requests:
      cpu:     250m
      memory:  128Mi
    """

    provider_type = os.getenv("LLM_PROVIDER", "ollama").lower()
    logger.info(f"Initializing log analyzer service with provider: \
                {provider_type}")

    if provider_type == "ollama":
        provider = OllamaProvider()
    elif provider_type == "gemini":
        provider = GeminiProvider()
    else:
        logger.error(f"Unsupported LLM provider specified: {provider_type}")
        provider = OllamaProvider()

    analyze_log(provider, sample_raw_log)
