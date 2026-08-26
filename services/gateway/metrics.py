"""Prometheus metrics.

No `repo`-style caller label here, unlike security-triage's: every label value below comes
from a closed set this module can see -- four service names, five outcomes -- so there is
nothing a request body could invent a new time series with.
"""

from prometheus_client import Counter, Histogram

# The one metric the day is about. `service="none"` is an unroutable ask, which is a
# legitimate answer and not an error -- splitting it out by name would put it in the same
# bucket as a backend outage.
ROUTES = Counter(
    "gw_routes",
    "/ask requests by which service they reached and how they ended",
    ["service", "outcome"],  # answered | accepted | needs_input | unroutable | failed
)

# A router that is always sure is a router that never doubts, and that is the failure mode
# worth catching: the floor can only help if the levels spread. It was a histogram over a
# float until 2026-08-26, when the measured run came back 1.00 or 0.00 and nothing else --
# nine buckets to describe a boolean. A counter over the three levels the field now holds
# says the same thing in three series, and cannot go out of range.
CONFIDENCE = Counter(
    "gw_router_confidence",
    "Confidence the router reported, including asks it then declined",
    ["level"],  # low | medium | high
)

# Seconds, and the buckets are wide because the same call is sub-second on Gemini and tens
# of seconds on CPU Ollama. This is the one model call a human waits on synchronously.
ROUTER_DURATION = Histogram(
    "gw_router_duration_seconds",
    "The classification call alone, not the backend that follows it",
    buckets=(0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 80),
)

# Separate from the router's, because "/ask is slow" has two very different causes and one
# histogram covering both cannot tell you which.
BACKEND_DURATION = Histogram(
    "gw_backend_duration_seconds",
    "Wall clock for the backend call /ask forwarded to",
    ["service"],
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)

# The passthrough, which never involves the model. Kept apart from ROUTES for that reason:
# mixing them would make the router's decline rate depend on how much plain proxying the
# CI jobs happen to be doing.
PROXY = Counter(
    "gw_proxy_requests",
    "Catch-all proxy requests by service and upstream status class",
    ["service", "status"],  # 2xx | 4xx | 5xx | unreachable
)

# "Requests were refused" is not actionable. `reason` is what tells a misconfigured caller
# apart from someone trying tokens.
REFUSALS = Counter(
    "gw_refusals",
    "Requests refused before any work was done, by which control refused",
    ["reason"],  # auth | unknown_service | bad_attachment
)

# provider.py accumulates these per-instance too; this is the same numbers somewhere a
# deploy can read them.
MODEL_TOKENS = Counter(
    "gw_model_tokens",
    "Tokens billed by the router, by direction",
    ["direction"],  # prompt | output
)
