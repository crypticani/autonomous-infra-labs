"""Prometheus metrics, declared once so nothing passes metric names as strings.

Counters are declared without the `_total` suffix -- prometheus_client appends it. They
live in the default process registry, coherent only because this service runs a single
uvicorn worker: two would mean two registries and Prometheus scraping whichever it hit.
"""

from prometheus_client import Counter, Gauge, Histogram

ANSWERS = Counter(
    "kc_answers",
    "Answers by outcome",
    ["outcome"],  # answered | ungrounded | refused
)

# 600s because a grounded answer measured 165-204s on CPU Ollama. The sub-second buckets
# are where retrieval lives, and the claim worth testing is that generation dominates it.
ANSWER_DURATION = Histogram(
    "kc_answer_duration_seconds",
    "Time spent per stage of answering",
    ["stage"],  # retrieval | generation
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 180, 240, 300, 600),
)

# Clustered around the floor, because the only decision this informs is where the floor
# belongs. The 0.64 default came from 21 offline cases; this gives production a vote.
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

# Read from live state at scrape time, never incremented: a tracked count drifts, and
# drift is the failure this exists to catch. /health once reported healthy while an empty
# bind mount shadowed every runbook, because alert chunks kept the count non-zero.
CHUNKS_INDEXED = Gauge("kc_chunks_indexed", "Rows in the Chroma collection")
SESSIONS_ACTIVE = Gauge("kc_sessions_active", "Slack threads with unexpired history")
ALERT_SYNC_AGE = Gauge(
    "kc_alert_sync_age_seconds", "Seconds since the last successful alert sync"
)

# An unlabelled gauge initialises to 0 and is exposed from the first scrape, so "never
# synced" would publish as zero seconds since the last sync -- the freshest possible
# reading and the opposite of the truth. NaN does not plot and does not satisfy a `> 300`
# alert comparison the way a fabricated 0 silently would.
ALERT_SYNC_AGE.set(float("nan"))
