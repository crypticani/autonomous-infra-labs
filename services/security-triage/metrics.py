"""Prometheus metrics -- Day 28.

Same shape as self-healing-agent/metrics.py and knowledge-copilot's before it, including
the two constraints both carry: counters are declared without the `_total` suffix because
prometheus_client appends it in the exposition, and the default process registry is only
coherent because this service runs a single uvicorn worker -- the same assumption `_runs`
and `_starts` in app.py already make.

What they measure here is different again. The copilot's metrics ask whether retrieval
found the right chunk; the agent's ask how often it decided not to act. This service is
**multi-tenant**, and that changes the question to *whose*: one repo's CI hammering the
endpoint, one repo's scan volume growing, one repo's risk trending up, are all invisible
in a global counter and all obvious with a `repo` label. Nothing else here has more than
one caller by design, so nothing else needed the label.

The tenancy is also why `_repo_label` exists -- see below. Every other label in this file
takes values from a Literal or a fixed tuple; `repo` is the one that arrives in a request
body from the internet.
"""

import os

from prometheus_client import Counter, Histogram

# A Prometheus label whose values come from an authenticated caller's request body is an
# unbounded-cardinality hazard, not a hypothetical one: a holder of one valid token can
# send a distinct `repo` per request and grow the registry until the process dies, and
# every scrape carries the whole thing along the way. Nothing downstream validates `repo`
# -- app.py takes it as a bare `str` because it is a display value, not an identifier.
#
# So the label is bounded here instead: the first ST_MAX_REPO_LABELS distinct repos seen
# get their own series and everything after that folds into `other`. The ceiling is well
# above the number of repos one deploy could plausibly onboard (each needs its own token
# added by hand), so in practice it never fires and the graphs stay per-repo.
#
# ponytail: the set never shrinks and first-seen wins, so a restart is what forgets a
# stale repo. Upgrade path: evict on last-seen if a deploy ever cycles through more repos
# than the cap, which would mean the token list is being managed automatically and this
# service has bigger changes to make than this one.
MAX_REPO_LABELS = int(os.getenv("ST_MAX_REPO_LABELS", "50"))

_repos: set[str] = set()


def repo_label(repo: str) -> str:
    """The value to use for the `repo` label -- the repo itself, or `other`."""
    if repo in _repos:
        return repo
    if len(_repos) < MAX_REPO_LABELS:
        _repos.add(repo)
        return repo
    return "other"


# Volume and reliability per tenant. `failed` climbing for one repo while the others stay
# flat is a repo whose envelopes break the parser; `failed` climbing across all of them is
# the model backend, which is the far commoner outage here (the laptop hosting Ollama is
# not always awake -- see the Readme).
RUNS = Counter(
    "st_runs",
    "Triage runs by how they ended",
    ["repo", "outcome"],  # accepted | done | failed
)

# Three stages, because the two gaps between them are both real failures that no single
# count would show. raw -> deduped is Day 22's dedup ratio, and it collapsing towards 1.0
# means fingerprinting stopped working. deduped -> triaged is the model dropping findings:
# triage.py refuses fingerprints that were never sent, so a batch that answers about four
# of the five it was given loses one here and nowhere else.
FINDINGS = Counter(
    "st_findings",
    "Findings by how far through the pipeline they got",
    ["repo", "stage"],  # raw | deduped | triaged
)

# The gate's actual behaviour, which is the number a team asks about first: how often does
# this thing block us. A repo at 100% fail has a threshold set wrong, and that is a
# conversation about `risk_threshold`, not about the model.
VERDICTS = Counter(
    "st_verdicts",
    "CI verdicts issued",
    ["repo", "verdict"],  # pass | fail
)

# A histogram and not a gauge: a gauge holds the last run's score, which for a repo that
# scans on every push is whatever landed most recently and says nothing about the trend.
# The buckets are risk.WEIGHTS' own boundaries rather than round numbers -- 1 is a single
# low, 4 a single medium, 15 a single high, 40 a single critical and also the default
# threshold -- so a bucket boundary means something rather than being decorative.
RISK_SCORE = Histogram(
    "st_risk_score",
    "Risk score of a completed run",
    ["repo"],
    buckets=(1, 4, 15, 40, 55, 70, 85, 100),
)

# Deliberately not labelled by repo. This one is a property of the *model*, not of a
# tenant: `needs_human` rising is the model declining more of what it is shown, and Day 23
# established that as the failure mode worth watching -- a model that satisfies every
# guard and declines everything looks identical to a clean run in `RUNS` and `VERDICTS`.
# Splitting it per repo would divide the signal across tenants and make the trend harder
# to see, for a question no tenant is asking.
PRIORITIES = Counter(
    "st_priorities",
    "Triage judgments by priority",
    ["priority"],  # critical | high | medium | low | needs_human
)

# Buckets reach an hour because a run is `ceil(findings / ST_BATCH_SIZE)` sequential model
# calls, and Day 27 measured one batch-of-5 call at 130-210s on CPU with 2x variance -- so
# the committed 43-finding fixture is nine calls and roughly half an hour, and a real
# monorepo is longer. The tail is the interesting end: /triage answers 202 immediately and
# the caller polls, so nobody sees a slow run except as a CI job that never finishes.
RUN_DURATION = Histogram(
    "st_run_duration_seconds",
    "Wall clock for one background triage run, successful or not",
    buckets=(30, 60, 120, 300, 600, 900, 1800, 3600),
)

# Day 27's token counters, exported. provider.py already accumulates these per-instance
# for bench.py to read as deltas; this is the same numbers where Prometheus can see them,
# which is the only place a *deploy* can answer "what did last week cost".
MODEL_TOKENS = Counter(
    "st_model_tokens",
    "Tokens billed, by direction",
    ["direction"],  # prompt | output
)

# The three controls app.py has carried since its first commit, made countable. This is
# the agent's GUARDRAIL_BLOCKS applied to a public endpoint: "requests were refused" is
# not actionable, and `reason` is what tells a misconfigured CI job (rate_limit, or
# body_size on a monorepo that outgrew the cap) apart from someone trying tokens.
REFUSALS = Counter(
    "st_refusals",
    "Requests refused before any work was done, by which control refused",
    ["reason"],  # auth | rate_limit | body_size
)
