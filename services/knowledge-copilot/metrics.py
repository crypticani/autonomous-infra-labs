"""Prometheus metrics, declared once so nothing passes metric names as strings.

Counter names are declared without the `_total` suffix: prometheus_client appends it in
the exposition, so `kc_answers` here is `kc_answers_total` on the wire.

These live in the default process registry, which is coherent only because this service
runs a single uvicorn worker -- the same constraint that stops two alert-sync loops
racing. Two workers would mean two registries and Prometheus scraping whichever one it
reached.
"""

from prometheus_client import Counter, Gauge, Histogram

ANSWERS = Counter(
    "kc_answers",
    "Answers by outcome",
    ["outcome"],  # answered | ungrounded | refused
)

# Buckets reach 600s because a grounded answer measured 165-204s against CPU-only
# Ollama. Sub-second buckets still earn their place: that is where retrieval lives, and
# the claim worth testing is that generation dominates it by three orders of magnitude.
ANSWER_DURATION = Histogram(
    "kc_answer_duration_seconds",
    "Time spent per stage of answering",
    ["stage"],  # retrieval | generation
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 180, 240, 300, 600),
)

# Buckets cluster tightly around the floor, because the only decision this metric
# informs is where the floor belongs. The 0.64 default was set from 21 offline cases;
# this is how production traffic gets a vote in that same question.
TOP_SIMILARITY = Histogram(
    "kc_retrieval_top_similarity",
    "Highest cosine any chunk scored, per question",
    buckets=(0.3, 0.4, 0.5, 0.55, 0.58, 0.60, 0.62, 0.64, 0.65, 0.70, 0.80, 0.90, 1.0),
)

SLACK_EVENTS = Counter(
    "kc_slack_events",
    "Inbound Slack events by outcome",
    ["outcome"],  # accepted | deduped_retry | bad_signature | not_a_mention
)

UPSTREAM_ERRORS = Counter(
    "kc_upstream_errors",
    "Failures of services this one depends on",
    ["provider"],  # ollama | gemini | embeddings | slack | alertmanager
)

# Read from live state at scrape time, never incremented. An incrementally-tracked chunk
# count drifts, and drift is the exact failure this metric exists to catch: /health once
# reported healthy while an empty bind mount shadowed every runbook, because alert-sync
# chunks kept the count non-zero.
CHUNKS_INDEXED = Gauge("kc_chunks_indexed", "Rows in the Chroma collection")
SESSIONS_ACTIVE = Gauge("kc_sessions_active", "Slack threads with unexpired history")
ALERT_SYNC_AGE = Gauge(
    "kc_alert_sync_age_seconds", "Seconds since the last successful alert sync"
)

# prometheus_client initialises an unlabelled gauge to 0 and exposes it from the first
# scrape onward, so "never synced" would publish as `0` -- zero seconds since the last
# sync, the freshest possible reading and the exact opposite of the truth. NaN is what
# Prometheus reads as "no data": it does not plot, and it does not satisfy a `> 300`
# alert comparison the way a fabricated 0 silently would.
ALERT_SYNC_AGE.set(float("nan"))
