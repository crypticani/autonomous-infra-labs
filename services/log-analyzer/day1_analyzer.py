import os
import logging
import requests
from abc import ABC, abstractmethod
from dotenv import load_dotenv
from google import genai

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class BaseLLMProvider(ABC):
    @abstractmethod
    def generate(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.1
    ) -> str:
        pass


class OllamaProvider(BaseLLMProvider):
    def __init__(self):
        self.model_name = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        logger.info(f"OllamaProvider initialized with model: {self.model_name}\
                    and base URL: {self.base_url}")

    def generate(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.1
    ) -> str:
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model_name,
            "system": system_prompt,
            "prompt": user_prompt,
            "temperature": temperature,
            "stream": False,
        }

        try:
            logger.info("Sending request to Ollama API")
            response = requests.post(url, json=payload, timeout=60)
            response.raise_for_status()
            return response.json().get("response", "")
        except requests.exceptions.RequestException as e:
            logger.exception(f"Failed to generate response from Ollama API: \
                             {e}")
            raise


class GeminiProvider(BaseLLMProvider):
    def __init__(self):
        if not os.getenv("GEMINI_API_KEY"):
            logger.error("GEMINI_API_KEY is not set in the environment \
                         variables.")
            print("GEMINI_API_KEY is not set in the environment variables.")

        self.model_name = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
        self.client = genai.Client()
        logger.info(f"GeminiProvider initialized with model: \
                     {self.model_name}")

    def generate(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.1
    ) -> str:
        try:
            logger.info("Sending request to Gemini API")
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=user_prompt,
                config={
                    "system_instruction": system_prompt,
                    "temperature": temperature,
                },
            )
            return response.text
        except Exception as e:
            logger.critical(f"Gemini API request failed: {e}")
            raise


def analyze_log(provider: BaseLLMProvider, error_log: str) -> None:
    system_instruction = (
        "You are a Senior DevOps Engineer with expertise in analyzing "
        "error logs. Analyze the following log.\n"
        "1. Identify the root cause of the failure.\n"
        "2. Explain it in concise, plain English.\n"
        "3. Suggest one concrete remediation step.\n"
        "Do not hallucinate or invent metrics not present in the log"
    )

    logger.info("Starting log analysis task...")

    try:
        result = provider.generate(
            system_prompt=system_instruction,
            user_prompt=f"RAW LOG:\n{error_log}",
            temperature=0.1,
        )

        print("\n=== AI Diagnostic Report ===")
        print(result.strip())
        print("=== End of Report ===\n")
        logger.info("Log analysis completed successfully.")
    except Exception as e:
        logger.critical(f"Log analysis failed: {e}")


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
