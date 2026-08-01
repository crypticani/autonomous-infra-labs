import os
import json
import logging
import requests
import uvicorn
from abc import ABC, abstractmethod
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field, ValidationError
from typing import Literal
from fastapi import FastAPI, HTTPException

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
            "Do not include markdown fences, code blocks, or commentary.\n"
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
                    variables")

        self.model_name = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
        self.client = genai.Client()
        logger.info(f"GeminiProvider initialized with model: {self.model_name}")

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


provider_type = os.getenv("LLM_PROVIDER", "ollama").lower()
logger.info(f"Initializing log analyzer service with provider: \
            {provider_type}")

if provider_type == "ollama":
    llm_provider = OllamaProvider()
elif provider_type == "gemini":
    llm_provider = GeminiProvider()
else:
    logger.error(f"Unsupported LLM provider specified: {provider_type}")
    llm_provider = OllamaProvider()


class LogRequest(BaseModel):
    raw_log: str = Field(min_length=15, description="The raw error log to analyze")


app = FastAPI(
    title="AI Log Analyzer Service",
    description="Turns raw infra logs into structured diagnostic data",
    version="1.0.0",
)


@app.get("/health")
def health_check():
    details = {"provider": provider_type, "model": llm_provider.model_name}

    if provider_type == "gemini" and not os.getenv("GEMINI_API_KEY"):
        details["error"] = "GEMINI_API_KEY is missing from environment"
        raise HTTPException(
            status_code=503, detail={"status": "degraded", "details": details}
        )

    return {"status": "healthy", "details": details}


@app.post("/analyze-log", response_model=LogAnalysis)
def analyze_log_endpoint(request: LogRequest):
    system_instruction = (
        "You are a Senior DevOps Engineer "
        "with expertise in analyzing error logs. "
        "Analyze the following log.\n"
        "1. Identify the root cause of the failure.\n"
        "2. Explain it in concise, plain English.\n"
        "3. Suggest one concrete remediation step.\n"
        "Do not hallucinate or invent metrics not present in the log"
    )

    logger.info("Received /analyze-log request")

    try:
        analysis = llm_provider.generate(
            system_prompt=system_instruction,
            user_prompt=f"RAW LOG:\n{request.raw_log}",
            temperature=0.1,
        )
        return analysis

    except ValueError as e:
        logger.exception(f"Data constraint failure: {e}")
        raise HTTPException(
            status_code=502,
            detail="Bad Gateway: Upstream AI returned malformed or invalid data.",
        )
    except requests.exceptions.Timeout as e:
        logger.exception(f"Upstream timeout: {e}")
        raise HTTPException(
            status_code=504, detail="Gateway Timeout. Upstream AI took long to respond."
        )
    except requests.exceptions.RequestException as e:
        logger.exception(f"Upstream connection error: {e}")
        raise HTTPException(
            status_code=503, detail="Service Unavailable: Upstream AI is unreachable."
        )
    except genai_errors.APIError as e:
        logger.exception(f"Gemini API error: {e}")
        raise HTTPException(
            status_code=502, detail="Bad Gateway: Upstream AI API encountered an error."
        )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7000)
