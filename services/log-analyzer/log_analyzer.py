import os
import json
import logging
import requests
import uvicorn
from abc import ABC, abstractmethod
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field, ValidationError
from typing import Literal
from fastapi import FastAPI, HTTPException, Response

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

LLM_REQUESTS_TOTAL = Counter(
    "llm_requests_total",
    "Total requests to LLM log analyzer by provider and status",
    ["provider", "status"],
)

LLM_REQUEST_DURATION_SECONDS = Histogram(
    "llm_request_duration_seconds",
    "End-to-end latency of /analyze-log requests in seconds",
    ["provider"],
)

LLM_TOKENS_TOTAL = Counter(
    "llm_tokens_total",
    "Total tokens consumed by LLM providers",
    ["provider", "token_type"],
)

ANALYSIS_SYSTEM_PROMPT = """
You are a Senior DevOps Engineer with expertise in analyzing error logs. 
Analyze the following log.
1. Identify the root cause of the failure.
2. Explain it in concise, plain English.
3. Suggest one concrete remediation step.
Do not hallucinate or invent metrics not present in the log.

CRITICAL INSTRUCTION - You MUST use the following severity rubric:
- CRITICAL: cascading impact across multiple services, unrecoverable data loss/corruption, security breach, or complete customer-facing outage with no fallback.
- HIGH: significant degradation or outage of a single service, no data loss, but trending toward escalation if unaddressed.
- MEDIUM: degraded performance or transient errors that self-recovered or have a working retry/fallback, limited user impact.
- LOW: isolated, non-recurring anomaly with no meaningful user impact.
"""

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
        logger.info(
            f"OllamaProvider initialized with model: {self.model_name}\
                and base URL: {self.base_url}"
        )

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
                logger.exception(
                    f"Failed to decode JSON from Ollama. Raw response: \
                        {raw_text}"
                )
                raise ValueError(f"Ollama returned malformed JSON: {e}")
            except ValidationError as e:
                logger.exception(
                    f"Pydantic validation failed for Ollama. Raw response: \
                        {raw_text}"
                )
                raise ValueError(
                    f"Ollama response failed schema constraints: \
                        {e}"
                )

        except requests.exceptions.RequestException as e:
            logger.exception(
                f"Failed to generate response from Ollama API: \
                    {e}"
            )
            raise


class GeminiProvider(BaseLLMProvider):
    def __init__(self):
        if not os.getenv("GEMINI_API_KEY"):
            logger.error(
                "GEMINI_API_KEY is not set in the environment \
                    variables"
            )

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
                logger.error(
                    f"Failed to decode JSON from Gemini. \
                        Raw response:\n{raw_text}"
                )
                raise ValueError(f"Gemini returned malformed JSON: {e}")
            except ValidationError as e:
                logger.error(
                    f"Pydantic validation failed for Gemini. \
                        Raw response:\n{raw_text}"
                )
                raise ValueError(
                    f"Gemini response failed schema constraints: \
                        {e}"
                )

        except genai_errors.APIError as e:
            logger.exception(
                f"Failed to generate response from Gemini API: {e}"
            )
            raise
        except (json.JSONDecodeError, ValidationError):
            raise


provider_type = os.getenv("LLM_PROVIDER", "ollama").lower()
logger.info(
    f"Initializing log analyzer service with provider: \
            {provider_type}"
)

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
    status = "healthy"
    details = []

    if provider_type == "gemini":
        if not os.getenv("GEMINI_API_KEY"):
            status = "degraded"
            details.append("GEMINI_API_KEY missing from environment")
        model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    elif provider_type == "ollama":
        model = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:1.5b")
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        try:
            r = requests.get(f"{base_url}/api/tags", timeout=2)
            if r.status_code != 200:
                status = "degraded"
                details.append(f"Ollama server returned HTTP {r.status_code}")
        except Exception as e:
            status = "degraded"
            details.append(f"Ollama server unreachable at {base_url}: {e}")
    else:
        status = "degraded"
        details.append(f"Unknown provider type: {provider_type}")
        model = "unknown"

    return {
        "status": status,
        "provider": provider_type,
        "model": model,
        "issues": details,
    }


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# The LLM provider clients (requests, google-genai) used inside `llm_provider.generate` 
# are blocking/synchronous. If we declared this as `async def`, the blocking HTTP call 
# to the LLM would stall FastAPI's main async event loop, freezing all other incoming requests. 
# By using standard `def`, Starlette automatically offloads this endpoint to a background threadpool, 
# keeping the service responsive to health checks and metrics scraping while the LLM \"thinks\".
@app.post("/analyze-log", response_model=LogAnalysis)
def analyze_log_endpoint(request: LogRequest):
    logger.info("Received /analyze-log request")

    try:
        analysis = llm_provider.generate(
            system_prompt=ANALYSIS_SYSTEM_PROMPT,
            user_prompt=f"RAW LOG:\n{request.raw_log}",
            temperature=0.1,
        )
        LLM_REQUESTS_TOTAL.labels(provider=provider_type, status="200").inc()
        return analysis

    except ValueError as e:
        logger.exception(f"Data constraint failure: {e}")
        LLM_REQUESTS_TOTAL.labels(provider=provider_type, status="502").inc()
        raise HTTPException(
            status_code=502,
            detail="Bad Gateway: Upstream AI returned malformed or invalid data.",
        )
    except requests.exceptions.Timeout as e:
        logger.exception(f"Upstream timeout: {e}")
        LLM_REQUESTS_TOTAL.labels(provider=provider_type, status="503").inc()
        raise HTTPException(
            status_code=504, detail="Gateway Timeout. Upstream AI took long to respond."
        )
    except requests.exceptions.RequestException as e:
        logger.exception(f"Upstream connection error: {e}")
        LLM_REQUESTS_TOTAL.labels(provider=provider_type, status="502").inc()
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
