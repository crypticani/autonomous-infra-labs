import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

import sessions
from connectors.alertmanager import ALERTMANAGER_URL, AlertmanagerError
from errors import UpstreamError
from ingest import sync_alerts
from llm import get_llm_provider
from metrics import (
    ALERT_SYNC_AGE,
    ANSWER_DURATION,
    ANSWERS,
    CHUNKS_INDEXED,
    SESSIONS_ACTIVE,
    SLACK_EVENTS,
    UPSTREAM_ERRORS,
)
from sessions import Turn
from slack_client import SlackError, post_message
from slack_events import (
    MIN_QUESTION_LENGTH,
    Mention,
    is_duplicate,
    parse_mention,
    slack_active,
    verify_signature,
)
from retrieval import (
    DEFAULT_K,
    SIMILARITY_FLOOR,
    EmptyIndexError,
    Hit,
    _index_cache,
    retrieve,
)
from store import open_collection

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logger = logging.getLogger(__name__)

# The lookbehind skips subscripts like argv[1] or ${nodes[0]}, but must NOT exclude a
# preceding "]": models write consecutive citations as [1][2], and dropping the second
# left an unresolvable marker in the prose while `grounded` still claimed true.
MARKER_RE = re.compile(r"(?<!\w)\[(\d+)\]")

# A fence carrying a language hint, on its own line. Anchored per-line so a stray ``` in
# prose is left alone, and requiring one or more characters so a closing fence never matches.
FENCE_LANG_RE = re.compile(r"^```[A-Za-z0-9_+#.-]+[ \t]*$", re.MULTILINE)

NOT_COVERED = "Not covered in the runbooks."

ACK_TEXT = "Looking through the runbooks — this takes a couple of minutes on CPU."
TOO_SHORT = "Ask me a fuller question and I'll search the runbooks."
INDEX_EMPTY = "The runbook index is empty — someone needs to run ingest.py."
MODEL_DOWN = "The model is unreachable, so I can't answer that right now."
FAILED = "Something went wrong answering that — check the service logs."

# One answer at a time. CPU Ollama serializes internally anyway and alert_sync_loop is
# already competing for it every 60 seconds, so two questions in flight means both take
# 400s instead of one taking 195s. The ack message covers the wait.
ANSWER_LOCK = asyncio.Semaphore(1)

# asyncio holds only a weak reference to a task, so a task with no strong reference of
# its own can be garbage-collected mid-answer. This set is that reference.
_tasks: set[asyncio.Task] = set()

ALERT_SYNC_INTERVAL = int(os.getenv("ALERT_SYNC_INTERVAL", "60"))
ALERT_SYNC_ENABLED = os.getenv("ALERT_SYNC_ENABLED", "true").lower() == "true"

# None until the first successful sync. Exported as the *absence* of the gauge rather
# than as 0, because zero seconds since last sync is the healthiest possible reading and
# would say the exact opposite of the truth.
_last_alert_sync: float | None = None


def grounded_system_prompt(now: datetime) -> str:
    """The grounding rules, plus a clock.

    Retrieved alert chunks carry absolute timestamps -- deliberately, because a
    relative one would churn the content hash on every poll. That makes "is this
    happening now" unanswerable unless the prompt says what "now" is.
    """
    return f"""You are an on-call SRE assistant. Answer operational questions using ONLY the runbook excerpts inside <context>.

The current time is {now.isoformat()}.

Rules:
- Use only facts stated in <context>. Never add commands, thresholds, or service names that are not there.
- Cite every claim with the id of the chunk it came from, written as [1], [2]. Cite chunk ids, never file names.
- A chunk beginning "Alert:" is live infrastructure state, not a runbook. Say whether it is firing or resolved, and when it started.
- If <context> does not answer the question, reply with exactly: Not covered in the runbooks.
- Be concise and practical: what is happening, then what to do. Plain prose, no preamble.
"""


class AskRequest(BaseModel):
    question: str = Field(min_length=10, description="The ops question to answer")
    k: int = Field(default=DEFAULT_K, ge=1, le=10)


class Source(BaseModel):
    marker: int
    source: str
    chunk_index: int
    score: float


class AskResponse(BaseModel):
    answer: str
    sources: list[Source]
    grounded: bool
    # "we produced an answer" and "that answer is grounded" are separate facts. Without
    # this field a caller can only tell a refusal from an uncited answer by string-
    # matching NOT_COVERED, since both carry grounded=false and no sources.
    answer_source: Literal["runbooks", "none"]


def build_context(hits: list[Hit]) -> str:
    blocks = [
        f'<chunk id="{marker}" source="{hit.source}" chunk_index="{hit.chunk_index}">\n'
        f"{hit.text}\n"
        "</chunk>"
        for marker, hit in enumerate(hits, start=1)
    ]
    return "<context>\n" + "\n".join(blocks) + "\n</context>"


def extract_markers(answer: str) -> list[int]:
    return list(dict.fromkeys(int(m) for m in MARKER_RE.findall(answer)))


def strip_markers(answer: str, markers: set[int]) -> str:
    if not markers:
        return answer

    def drop(match: re.Match) -> str:
        return "" if int(match.group(1)) in markers else match.group(0)

    cleaned = MARKER_RE.sub(drop, answer)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return re.sub(r"\s+([.,;:])", r"\1", cleaned).strip()


def ground_answer(raw_answer: str, hits: list[Hit]) -> tuple[str, list[Source], bool]:
    valid = set(range(1, len(hits) + 1))
    cited = extract_markers(raw_answer)
    unresolvable = {marker for marker in cited if marker not in valid}

    if unresolvable:
        logger.warning(f"model invented citations {sorted(unresolvable)}; stripping")

    answer = strip_markers(raw_answer, unresolvable)
    sources = [
        Source(
            marker=marker,
            source=hits[marker - 1].source,
            chunk_index=hits[marker - 1].chunk_index,
            score=round(hits[marker - 1].score, 3),
        )
        for marker in cited
        if marker in valid
    ]
    grounded = bool(hits) and bool(sources) and not unresolvable
    return answer, sources, grounded


def alert_sync_tick() -> None:
    """One sync, blocking. Never raises: a failed poll is logged and retried."""
    global _last_alert_sync
    try:
        plan = sync_alerts()
    except AlertmanagerError as e:
        # Expected and recoverable -- appsrv reboots, the tailnet blips. The write was
        # skipped, so the index still holds the last state we actually observed.
        UPSTREAM_ERRORS.labels(provider="alertmanager").inc()
        logger.warning(f"alert sync skipped: {e}")
        return
    except Exception:
        logger.exception("alert sync failed")
        return

    # Set before the write check, not after: a poll that found nothing to change still
    # observed live state, and is not a stale sync.
    _last_alert_sync = time.time()

    if plan.to_upsert or plan.to_delete:
        # The writer knows when it wrote, which is the whole fix for retrieval's
        # (name, count) cache key: one alert resolving as another fires leaves the
        # count identical and the content completely different. The CLI-writes-while-
        # the-app-runs case keeps its existing "restart after re-ingest" caveat.
        _index_cache.clear()
        logger.info(
            f"alert sync: +{len(plan.to_add)} ~{len(plan.to_update)} "
            f"-{len(plan.to_delete)} ={len(plan.unchanged)}"
        )


async def alert_sync_loop() -> None:
    while True:
        # to_thread, not a direct call: sync_alerts blocks on Ollama embeddings for
        # seconds at a time, and on the event loop that stalls every concurrent
        # /ask-runbook request behind it.
        await asyncio.to_thread(alert_sync_tick)
        await asyncio.sleep(ALERT_SYNC_INTERVAL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = None
    if ALERT_SYNC_ENABLED:
        # Single worker assumed: two uvicorn workers means two loops racing on the
        # same writes. Documented in the Readme rather than solved with a lock -- a
        # distributed lock for a single-node deployment is machinery for nobody.
        task = asyncio.create_task(alert_sync_loop())
        logger.info(f"alert sync every {ALERT_SYNC_INTERVAL}s from {ALERTMANAGER_URL}")
    if not slack_active():
        logger.warning("slack disabled: SLACK_ENABLED off, or a secret is unset")
    yield
    if task:
        task.cancel()


app = FastAPI(
    title="Knowledge Copilot",
    description="Answers ops questions from the runbook corpus, with citations",
    version="1.0.0",
    lifespan=lifespan,
)


def answer_question(
    question: str, k: int = DEFAULT_K, turns: Sequence[Turn] = ()
) -> AskResponse:
    """Retrieve, ground, answer. Raises for the caller to map to its own protocol.

    `question` is always the raw new question. History shapes what gets retrieved and
    what gets prompted at two different depths -- see sessions.py. With no history both
    reduce to identity, which is why /ask-runbook's behaviour is unchanged.
    """
    provider, collection = open_collection()

    started = time.perf_counter()
    hits = retrieve(
        sessions.retrieval_query(list(turns), question),
        provider=provider,
        collection=collection,
        k=k,
    )
    ANSWER_DURATION.labels(stage="retrieval").observe(time.perf_counter() - started)

    if not hits:
        logger.info("nothing cleared the similarity floor; refusing")
        ANSWERS.labels(outcome="refused").inc()
        return AskResponse(
            answer=NOT_COVERED, sources=[], grounded=False, answer_source="none"
        )

    started = time.perf_counter()
    raw = get_llm_provider().generate(
        grounded_system_prompt(datetime.now(timezone.utc)),
        f"{sessions.prompt_history(list(turns))}{build_context(hits)}\n"
        f"<question>\n{question}\n</question>",
    )
    ANSWER_DURATION.labels(stage="generation").observe(time.perf_counter() - started)

    answer, sources, grounded = ground_answer(raw, hits)
    ANSWERS.labels(outcome="answered" if grounded else "ungrounded").inc()
    logger.info(f"answered grounded={grounded} sources={[s.marker for s in sources]}")
    return AskResponse(
        answer=answer, sources=sources, grounded=grounded, answer_source="runbooks"
    )


@app.post("/ask-runbook", response_model=AskResponse)
def ask_runbook(request: AskRequest):
    logger.info(f"/ask-runbook k={request.k} q={request.question!r}")
    try:
        return answer_question(request.question, k=request.k)
    except EmptyIndexError as e:
        logger.error(f"{e}")
        raise HTTPException(status_code=503, detail=f"{e}")
    except UpstreamError as e:
        UPSTREAM_ERRORS.labels(provider=e.provider).inc()
        logger.warning(f"{e}")
        raise HTTPException(status_code=e.status, detail=str(e))


def strip_fence_languages(text: str) -> str:
    """Drop the language hint from code fences.

    Slack's mrkdwn has no language-hinted fences, so ```sh renders as a code block whose
    first visible line is the word "sh". That showed up in every answer containing a
    command. Closing fences are bare and stay untouched: the pattern requires at least
    one character after the backticks.
    """
    return FENCE_LANG_RE.sub("```", text)


def format_sources(sources: list[Source]) -> str:
    """Slack mrkdwn, not markdown: underscores italicise and link syntax does nothing."""
    if not sources:
        return ""
    lines = [f"_[{s.marker}] {s.source} #{s.chunk_index} · {s.score}_" for s in sources]
    return "\n" + "\n".join(lines)


async def post(mention: Mention, text: str) -> None:
    """Post, and never let a Slack failure escape into the task's traceback."""
    try:
        await asyncio.to_thread(post_message, mention.channel, mention.thread_ts, text)
    except SlackError:
        UPSTREAM_ERRORS.labels(provider="slack").inc()
        logger.exception("slack: could not post to the thread")


async def answer_and_post(mention: Mention) -> None:
    """The slow half, off the request path.

    Every exit posts something. At 195 seconds a silent bot is indistinguishable from a
    broken one, and the person waiting has no way to tell which.
    """
    if len(mention.question) < MIN_QUESTION_LENGTH:
        await post(mention, TOO_SHORT)
        return

    await post(mention, ACK_TEXT)
    turns = sessions.history(mention.thread_ts, now=time.time())

    try:
        async with ANSWER_LOCK:
            # to_thread, not a direct call: answer_question blocks on Ollama for three
            # minutes, and on the event loop that stalls the alert sync and every other
            # request behind it.
            result = await asyncio.to_thread(
                answer_question, mention.question, DEFAULT_K, turns
            )
    except EmptyIndexError:
        logger.exception("slack: index empty")
        await post(mention, INDEX_EMPTY)
        return
    except UpstreamError:
        logger.exception("slack: upstream failed")
        await post(mention, MODEL_DOWN)
        return
    except Exception:
        logger.exception("slack: unexpected failure")
        await post(mention, FAILED)
        return

    if result.answer_source == "runbooks":
        # A refusal is deliberately not remembered: carrying a question that retrieved
        # nothing would only dilute the next turn's retrieval query.
        sessions.append(
            mention.thread_ts,
            Turn(mention.question, result.answer),
            now=time.time(),
        )
    await post(
        mention,
        strip_fence_languages(result.answer) + format_sources(result.sources),
    )


def spawn(mention: Mention) -> None:
    """Fire the slow half and keep a strong reference to it -- see _tasks."""
    task = asyncio.create_task(answer_and_post(mention))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@app.post("/slack/events")
async def slack_events(request: Request):
    if not slack_active():
        raise HTTPException(status_code=404, detail="Slack is not configured")

    # The raw bytes, before any parsing: re-serialising changes whitespace and key
    # order, and the HMAC then never matches.
    body = await request.body()
    if not verify_signature(
        body,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        request.headers.get("X-Slack-Signature", ""),
        now=time.time(),
    ):
        SLACK_EVENTS.labels(outcome="bad_signature").inc()
        logger.warning("slack: rejected an unsigned, forged or stale request")
        raise HTTPException(status_code=401, detail="bad signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        # 200, because a 500 makes Slack retry a body that can never parse.
        logger.warning("slack: unparseable body")
        return Response(status_code=200)

    # Slack's one-time handshake when the Request URL is saved in the app config.
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    event = payload.get("event", {})
    if event.get("type") != "app_mention":
        SLACK_EVENTS.labels(outcome="not_a_mention").inc()
        return Response(status_code=200)

    # Before any work is spawned, not after: the point is to never start the second
    # and third 195-second job.
    event_id = payload.get("event_id", "")
    if is_duplicate(event_id, now=time.time()):
        SLACK_EVENTS.labels(outcome="deduped_retry").inc()
        logger.info(f"slack: duplicate event {event_id}, dropped")
        return Response(status_code=200)

    if mention := parse_mention(event):
        SLACK_EVENTS.labels(outcome="accepted").inc()
        logger.info(f"slack: {mention.thread_ts} q={mention.question!r}")
        spawn(mention)
    return Response(status_code=200)


@app.get("/metrics")
def metrics():
    """Prometheus scrape target.

    Unauthenticated on purpose: Prometheus reaches this over loopback on appsrv, and a
    bearer token in a scrape config is a secret in a third place buying nothing.
    """
    try:
        _, collection = open_collection()
        CHUNKS_INDEXED.set(collection.count())
    except Exception:
        # A scrape must never 500. An unreachable collection is what /health is for;
        # here it only means this one gauge has nothing to say this time round.
        logger.exception("metrics: collection unavailable")

    SESSIONS_ACTIVE.set(sessions.active_count(time.time()))
    # Set on every scrape, including the unknown case. Skipping the set would leave the
    # gauge holding whatever it last reported, which is how a stale reading outlives the
    # state that produced it. NaN is Prometheus's "no data".
    ALERT_SYNC_AGE.set(
        float("nan") if _last_alert_sync is None else time.time() - _last_alert_sync
    )

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health_check():
    issues: list[str] = []
    provider_name = model = embedding_provider = collection_name = "unknown"
    indexed = 0

    try:
        llm = get_llm_provider()
        provider_name, model = llm.name, llm.model_name
    except Exception as e:
        llm = None
        issues.append(f"LLM provider unavailable: {e}")

    # Constructing a provider does no I/O, so without this the endpoint reports healthy
    # while appsrv is down or the model was never pulled -- which reaches callers as a
    # 502 mid-answer instead of a degraded health check.
    if llm is not None and llm.name == "ollama":
        try:
            tags = requests.get(f"{llm.base_url}/api/tags", timeout=2)
            tags.raise_for_status()
            pulled = {entry["name"] for entry in tags.json().get("models", [])}
            if model not in pulled and f"{model}:latest" not in pulled:
                issues.append(f"model {model!r} is not pulled on {llm.base_url}")
        except requests.exceptions.RequestException as e:
            issues.append(f"Ollama unreachable at {llm.base_url}: {e}")

    try:
        embed_provider, collection = open_collection()
        embedding_provider, collection_name = embed_provider.name, collection.name
        indexed = collection.count()
        if indexed == 0:
            issues.append(f"collection {collection_name!r} is empty; run ingest.py")
    except Exception as e:
        issues.append(f"collection unavailable: {e}")

    return {
        "status": "degraded" if issues else "healthy",
        "provider": provider_name,
        "model": model,
        "embedding_provider": embedding_provider,
        "collection": collection_name,
        "chunks_indexed": indexed,
        "similarity_floor": SIMILARITY_FLOOR,
        "slack": "active" if slack_active() else "disabled",
        "issues": issues,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("COPILOT_PORT", "7100")))
