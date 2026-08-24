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

import metrics
from errors import TriageProviderError

load_dotenv()

logger = logging.getLogger(__name__)

# Its own variable as of Day 27, no longer sharing knowledge-copilot's LLM_TIMEOUT. Two
# reasons, and the measurement forced both.
#
# 300s was not enough. One batch of 5 findings measured 158.7s, 208.4s, and then >300s --
# the same five findings, the same batch size, timing out on the third attempt. A CPU
# under sustained load throttles, and a batch the model declines writes longer
# explanations than one it judges, so output tokens (which set the wall clock here at
# ~2.5-3 tok/s) vary with the *answer*, not just the input. A timeout is the worst
# available outcome: the full 300s is spent and nothing comes back, so the batch is
# re-run from scratch. 600s buys the headroom that turns a wasted 300s into a slow
# success, and nothing here is latency-sensitive enough to prefer the failure -- /triage
# returns a run_id immediately and joins the work in the background (Day 25).
#
# And these two services genuinely want different numbers: a copilot answer is one call a
# human is waiting on, a triage batch is one of many inside a background run. One shared
# knob could only ever be right for one of them. Every other setting here already carries
# the ST_ prefix; this one was the outlier.
LLM_TIMEOUT = int(os.getenv("ST_LLM_TIMEOUT", "600"))

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

    # Cumulative tokens across every generate() this provider has served. Attributes
    # rather than a second return value because triage.py consumes generate()'s text and
    # has to keep working without knowing tokens exist -- Day 27 needs the numbers, the
    # pipeline does not. Cumulative rather than per-call so a caller that makes several
    # calls (bench.py sweeping a batch size, or self-healing-agent's multi-turn diagnosis
    # using the same shape) reads one delta instead of summing. Same read-by-delta
    # contract log-analyzer's LLM_TOKENS_TOTAL counter already has.
    #
    # Plain ints, not a dict: `self.prompt_tokens += n` rebinds on the instance, so a
    # class-level default cannot be silently shared the way a mutable one would be.
    #
    # ponytail: unsynchronised on an lru_cache'd singleton, so concurrent /triage runs
    # interleave their increments. Totals stay right, a delta straddling another run's
    # calls does not; bench.py is sequential and is the only reader that takes deltas.
    prompt_tokens = 0
    output_tokens = 0

    def _count(self, prompt: int, output: int) -> None:
        """Record one call's usage in both places it has to land -- Day 28.

        Here rather than at each provider's own increment site, because there are two of
        those and adding the Prometheus half to one and not the other is a silent
        undercount that nothing fails on. The two readers want genuinely different
        things: bench.py takes deltas off the attributes within one process, and the
        /metrics counters survive it into a deploy nobody is watching.
        """
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
        # 7b, and the size is load-bearing rather than a default nobody measured. The
        # plan called for starting at 1.5b and letting Day 27 judge whether bigger was
        # worth the latency; Day 23 measured it early because 1.5b turned out not to
        # triage at all -- five byte-identical boilerplate explanations, needs_human for
        # everything, all guards satisfied and all of it worthless. 7b answers the same
        # prompt correctly at ~3.3x the wall-clock. Slow and right beats fast and useless;
        # see the measurement table in Readme.md.
        self.model_name = os.getenv("ST_OLLAMA_MODEL", "qwen2.5-coder:7b")
        # ST_OLLAMA_BASE_URL first, falling back to the shared one -- Day 28, and it is a
        # deploy problem rather than a preference. appsrv runs all four services off a
        # single `.env`, and this is the only one whose backend moved to a laptop over
        # Tailscale. Editing the shared `OLLAMA_BASE_URL` to point there would silently
        # take log-analyzer and knowledge-copilot with it, onto a host that is asleep most
        # of the time, for no reason either of them asked for. The override keeps that
        # blast radius to one service; the fallback means a host where every service does
        # share one backend needs no new variable at all.
        #
        # Third time this shape has been needed here (ST_GEMINI_MODEL for quota
        # isolation, ST_LLM_TIMEOUT for a background run vs. a waiting human, now this):
        # one shared knob can only ever be right for one of its readers.
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

        body = response.json()
        # prompt_eval_count is the system prompt plus the batch; eval_count is the JSON
        # it wrote back. The first is what makes batching pay -- the system prompt is
        # charged once per call regardless of how many findings ride along.
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
        # Its own variable, not GEMINI_MODEL or SHA_GEMINI_MODEL: Gemini's free-tier
        # quota is scoped per-project-per-model, and Day 21 already lost a diagnosis to
        # two services silently sharing one model name's bucket. That is also why this
        # moved to 3.7 alone on Day 27 rather than every service moving with it -- a repo
        # where all four name the same model has one bucket for four workloads, and the
        # isolation is worth more here than uniformity.
        #
        # 3.7-flash over 3.6: cheaper per token on the paid tier, and verified reachable
        # on this key on 2026-08-22 before anything was pointed at it.
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

        # usage_metadata is None when the request never reached billing at all -- a safety
        # block, say -- so this is a real branch, not defensive noise.
        if usage := response.usage_metadata:
            # thoughts_token_count belongs in output, not dropped: reasoning tokens are
            # billed at the output rate. Measured on 2026-08-22 while checking the quota
            # was still alive -- "reply with the single word ok" spent 119 thinking tokens
            # to produce 1 candidate token, so counting candidates alone would have
            # understated that call's output cost by 120x and made Gemini look free.
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
    # Ollama, unlike self-healing-agent's default: no quota ceiling, and scan data
    # (someone else's repo layout and dependency versions) never leaves the tailnet.
    provider_type = os.getenv("ST_LLM_PROVIDER", "ollama").lower()
    if provider_type == "ollama":
        return OllamaProvider()
    if provider_type == "gemini":
        return GeminiProvider()
    raise ValueError(f"Unsupported ST_LLM_PROVIDER: {provider_type!r}")
