"""Prometheus metrics, declared once so nothing passes metric names as strings.

Counters are declared without the `_total` suffix because prometheus_client appends it,
and the default registry is only coherent because this service runs a single worker.

The copilot's metrics measure quality. These measure restraint -- every counter below
answers a version of "how often did this thing decide not to act".
"""

from prometheus_client import Counter, Histogram

# `duplicate` climbing while `accepted` stays flat is the healthy shape for a flapping
# alert. The two moving together means dedup is broken.
ALERTS_RECEIVED = Counter(
    "sha_alerts_received",
    "Alerts arriving on the Alertmanager webhook, by what was done with them",
    ["outcome"],  # accepted | resolved | duplicate
)

DIAGNOSES = Counter(
    "sha_diagnoses",
    "Diagnosis attempts by how they ended",
    # complete: the model called submit_diagnosis. incomplete: it ran out of iterations.
    # blocked: a guardrail refused before any model call. failed: an upstream did.
    ["outcome"],
)

# 600s because a diagnosis is six to ten round trips with a growing transcript. The
# interesting question is how close the tail runs to the MAX_ITERATIONS ceiling.
DIAGNOSIS_DURATION = Histogram(
    "sha_diagnosis_duration_seconds",
    "Wall clock for one diagnosis, successful or not",
    buckets=(5, 10, 20, 30, 60, 90, 120, 180, 300, 600),
)

# The `guard` label is the point: "the agent refused something" is not actionable,
# "the breaker refused something" is a page.
GUARDRAIL_BLOCKS = Counter(
    "sha_guardrail_blocks",
    "Refusals, by which rule refused",
    [
        "guard"
    ],  # namespace | replica_floor | live_replicas | rate_limit | breaker | llm_calls
)

# proposed vs executed is how much the agent wanted to do against how much a human let
# it -- the number that says whether this is trustworthy yet.
PROPOSALS = Counter(
    "sha_proposals",
    "Proposal lifecycle transitions",
    ["state"],  # proposed | approved | rejected | executed | failed | expired | blocked
)

# A retry firing constantly is a provider to reconsider; one that never moves is a retry
# nobody has evidence works.
MODEL_RETRIES = Counter(
    "sha_model_retries",
    "Transient model failures retried rather than surfaced",
    ["status"],  # the HTTP status the model API returned
)

# What a diagnosis costs. DIAGNOSIS_DURATION says how long the user waited, this says
# what was spent. Each turn re-sends the whole transcript, so tokens grow
# super-linearly in turn count.
#
# "output" includes reasoning tokens: a tool-selection turn writes almost no prose, so
# nearly all of its output is thinking.
MODEL_TOKENS = Counter(
    "sha_model_tokens",
    "Tokens billed, by direction",
    ["direction"],  # prompt | output
)
