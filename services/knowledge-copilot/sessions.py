"""Day 13: thread history for the Slack interface.

A Slack thread is a session. Nothing here does I/O and nothing here reads a clock --
`now` is a parameter, so TTL eviction tests against a fixed timeline instead of sleeping
for an hour.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

SESSION_TTL = int(os.getenv("SESSION_TTL", "3600"))
SESSION_MAX_TURNS = int(os.getenv("SESSION_MAX_TURNS", "4"))

# Two depths, on purpose. The retrieval query is the fragile one: every extra word
# shifts BM25's term weighting and drags the dense vector toward the corpus centroid,
# so it gets exactly one prior question. The answer prompt tolerates more.
HISTORY_TURNS_IN_QUERY = 1
HISTORY_TURNS_IN_PROMPT = 2


@dataclass(frozen=True)
class Turn:
    question: str
    answer: str


# thread_ts -> (last_used_at, turns). Module-level and therefore per-process: a restart
# forgets every thread, which is the documented tradeoff. Persistence for a single-node
# demo bot is machinery for nobody.
_sessions: dict[str, tuple[float, list[Turn]]] = {}


def evict(now: float, ttl: int = SESSION_TTL) -> None:
    stale = [ts for ts, (used, _) in _sessions.items() if now - used > ttl]
    for thread_ts in stale:
        del _sessions[thread_ts]


def history(thread_ts: str, now: float) -> list[Turn]:
    evict(now)
    entry = _sessions.get(thread_ts)
    return list(entry[1]) if entry else []


def append(thread_ts: str, turn: Turn, now: float) -> None:
    _, turns = _sessions.get(thread_ts, (now, []))
    _sessions[thread_ts] = (now, [*turns, turn][-SESSION_MAX_TURNS:])


def retrieval_query(turns: list[Turn], question: str) -> str:
    """What to actually search for.

    Only prior *questions* are carried, never prior answers: an answer is 400 words of
    prose that would swamp the six words that matter.
    """
    prior = [turn.question for turn in turns[-HISTORY_TURNS_IN_QUERY:]]
    return " ".join([*prior, question])


def prompt_history(turns: list[Turn]) -> str:
    """Prior turns as a prompt block. Empty string when there are none, so the caller
    can concatenate unconditionally."""
    if not turns:
        return ""
    blocks = [
        f"<turn>\n<question>{turn.question}</question>\n"
        f"<answer>{turn.answer}</answer>\n</turn>"
        for turn in turns[-HISTORY_TURNS_IN_PROMPT:]
    ]
    return "<history>\n" + "\n".join(blocks) + "\n</history>\n"
