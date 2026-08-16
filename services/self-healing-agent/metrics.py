"""Prometheus metrics, declared once so nothing passes metric names as strings.

Same shape as knowledge-copilot's metrics.py, including its constraint: counters are
declared without the `_total` suffix because prometheus_client appends it in the
exposition, and the default process registry is only coherent because this service runs a
single uvicorn worker -- the same assumption alerts._seen and approvals._proposals make.

What these are *for* is different from the copilot's, though. There, metrics measure
quality: is retrieval finding the right chunk. Here they measure restraint. Every counter
below answers a version of "how often did this thing decide not to act", because from Day
20 on nobody is watching it decide.
"""

from prometheus_client import Counter, Histogram

# The webhook's own arithmetic. `duplicate` climbing while `accepted` stays flat is the
# healthy shape for a flapping alert; the two moving together means dedup is broken and
# the model budget is being spent on the same alert over and over.
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

# Buckets reach 600s because a diagnosis is six to ten model calls, not one, and each is a
# full round trip with a growing transcript. The interesting question is not the median --
# it is how close the tail runs to the MAX_ITERATIONS ceiling, since a diagnosis that
# times out somewhere upstream is indistinguishable from one that was merely slow.
DIAGNOSIS_DURATION = Histogram(
    "sha_diagnosis_duration_seconds",
    "Wall clock for one diagnosis, successful or not",
    buckets=(5, 10, 20, 30, 60, 90, 120, 180, 300, 600),
)

# Named in errors.py on Day 15, before there was anywhere to put it. The `guard` label is
# the whole point: "the agent refused something" is not actionable, and "the breaker
# refused something" is a page.
GUARDRAIL_BLOCKS = Counter(
    "sha_guardrail_blocks",
    "Refusals, by which rule refused",
    [
        "guard"
    ],  # namespace | replica_floor | live_replicas | rate_limit | breaker | llm_calls
)

# The ratio worth graphing in this service. proposed vs executed is how much the agent
# wanted to do against how much a human let it, and it is the number that says whether
# this thing is trustworthy yet.
PROPOSALS = Counter(
    "sha_proposals",
    "Proposal lifecycle transitions",
    ["state"],  # proposed | approved | rejected | executed | failed | expired | blocked
)

# Day 20's retry, made visible. A retry that fires constantly is a provider to reconsider,
# and a counter that never moves at all is a retry nobody has evidence works.
MODEL_RETRIES = Counter(
    "sha_model_retries",
    "Transient model failures retried rather than surfaced",
    ["status"],  # the HTTP status the model API returned
)
