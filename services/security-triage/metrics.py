"""Prometheus metrics."""

import os

from prometheus_client import Counter, Histogram

# `repo` arrives in a request body and nothing validates it -- it is a display value, not
# an identifier -- so a holder of one valid token could otherwise mint a new time series
# per request until the process dies. The first MAX_REPO_LABELS distinct repos get their
# own series and the rest fold into `other`; the ceiling is far above what any deploy
# could onboard by hand, so in practice it never fires.
#
# ponytail: first-seen wins and the set never shrinks, so a restart is what forgets a
# stale repo. Upgrade path is eviction on last-seen.
MAX_REPO_LABELS = int(os.getenv("ST_MAX_REPO_LABELS", "50"))

_repos: set[str] = set()


def repo_label(repo: str) -> str:
    """The repo itself, or `other` once the cap is reached."""
    if repo in _repos:
        return repo
    if len(_repos) < MAX_REPO_LABELS:
        _repos.add(repo)
        return repo
    return "other"


# `failed` climbing for one repo is a broken envelope; climbing across all of them is the
# model backend, which is the commoner outage here.
RUNS = Counter(
    "st_runs",
    "Triage runs by how they ended",
    ["repo", "outcome"],  # accepted | done | failed
)

# Both gaps matter: raw -> deduped is the dedup ratio, and deduped -> triaged is the
# model answering about fewer findings than it was given.
FINDINGS = Counter(
    "st_findings",
    "Findings by how far through the pipeline they got",
    ["repo", "stage"],  # raw | deduped | triaged
)

# The first question a team asks: how often does this thing block us.
VERDICTS = Counter(
    "st_verdicts",
    "CI verdicts issued",
    ["repo", "verdict"],  # pass | fail
)

# A histogram, not a gauge: a gauge holds the last run's score, which says nothing about
# the trend. Buckets are risk.WEIGHTS' own boundaries -- one low, one medium, one high,
# one critical (and the default threshold) -- so each means something.
RISK_SCORE = Histogram(
    "st_risk_score",
    "Risk score of a completed run",
    ["repo"],
    buckets=(1, 4, 15, 40, 55, 70, 85, 100),
)

# Deliberately not per-repo: the needs_human rate is a property of the model, and
# splitting it across tenants would divide the signal for a question nobody asks.
PRIORITIES = Counter(
    "st_priorities",
    "Triage judgments by priority",
    ["priority"],  # critical | high | medium | low | needs_human
)

# Buckets reach an hour: a run is ceil(findings / ST_BATCH_SIZE) sequential calls at
# 130-210s each. The tail is the interesting end, since nobody watches a slow run except
# as a CI job that never finishes.
RUN_DURATION = Histogram(
    "st_run_duration_seconds",
    "Wall clock for one background triage run, successful or not",
    buckets=(30, 60, 120, 300, 600, 900, 1800, 3600),
)

# provider.py already accumulates these per-instance for bench.py; this is the same
# numbers somewhere a deploy can read them.
MODEL_TOKENS = Counter(
    "st_model_tokens",
    "Tokens billed, by direction",
    ["direction"],  # prompt | output
)

# "Requests were refused" is not actionable. `reason` is what tells a misconfigured CI
# job apart from someone trying tokens.
REFUSALS = Counter(
    "st_refusals",
    "Requests refused before any work was done, by which control refused",
    ["reason"],  # auth | rate_limit | body_size
)
