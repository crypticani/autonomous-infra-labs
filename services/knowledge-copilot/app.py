import logging
import os
import re
from typing import Literal

import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from llm import UpstreamError, get_llm_provider
from retrieval import (
    DEFAULT_K,
    SIMILARITY_FLOOR,
    EmptyIndexError,
    Hit,
    open_collection,
    retrieve,
)

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

NOT_COVERED = "Not covered in the runbooks."

GROUNDED_SYSTEM_PROMPT = """You are an on-call SRE assistant. Answer operational questions using ONLY the runbook excerpts inside <context>.

Rules:
- Use only facts stated in <context>. Never add commands, thresholds, or service names that are not there.
- Cite every claim with the id of the chunk it came from, written as [1], [2]. Cite chunk ids, never file names.
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


app = FastAPI(
    title="Knowledge Copilot",
    description="Answers ops questions from the runbook corpus, with citations",
    version="1.0.0",
)


@app.post("/ask-runbook", response_model=AskResponse)
def ask_runbook(request: AskRequest):
    logger.info(f"/ask-runbook k={request.k} q={request.question!r}")

    try:
        provider, collection = open_collection()
        hits = retrieve(
            request.question, provider=provider, collection=collection, k=request.k
        )
        if not hits:
            logger.info("nothing cleared the similarity floor; refusing")
            return AskResponse(
                answer=NOT_COVERED, sources=[], grounded=False, answer_source="none"
            )

        raw = get_llm_provider().generate(
            GROUNDED_SYSTEM_PROMPT,
            f"{build_context(hits)}\n<question>\n{request.question}\n</question>",
        )
    except EmptyIndexError as e:
        logger.error(f"{e}")
        raise HTTPException(status_code=503, detail=f"{e}")
    except UpstreamError as e:
        logger.warning(f"{e}")
        raise HTTPException(status_code=e.status, detail=str(e))

    answer, sources, grounded = ground_answer(raw, hits)
    logger.info(f"answered grounded={grounded} sources={[s.marker for s in sources]}")
    return AskResponse(
        answer=answer, sources=sources, grounded=grounded, answer_source="runbooks"
    )


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
        "issues": issues,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("COPILOT_PORT", "7100")))
