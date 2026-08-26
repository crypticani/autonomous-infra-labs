"""Prometheus metrics.

Every label value comes from a closed set, so unlike security-triage's `repo` there is
nothing here a request body could mint a new series with.
"""

from prometheus_client import Counter, Histogram

# `service="none"` is an unroutable ask -- a legitimate answer, kept out of the same
# bucket as a backend outage.
ROUTES = Counter(
    "gw_routes",
    "/ask requests by which service they reached and how they ended",
    ["service", "outcome"],  # answered | accepted | needs_input | unroutable | failed
)

# A router that is always sure never doubts, and the floor only helps if levels spread.
CONFIDENCE = Counter(
    "gw_router_confidence",
    "Confidence the router reported, including asks it then declined",
    ["level"],  # low | medium | high
)

# Wide buckets: sub-second on Gemini, tens of seconds on CPU Ollama.
ROUTER_DURATION = Histogram(
    "gw_router_duration_seconds",
    "The classification call alone, not the backend that follows it",
    buckets=(0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 80),
)

# Separate from the router's: "/ask is slow" has two causes and one histogram hides which.
BACKEND_DURATION = Histogram(
    "gw_backend_duration_seconds",
    "Wall clock for the backend call /ask forwarded to",
    ["service"],
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)

# Apart from ROUTES: mixing them would make the decline rate depend on proxy volume.
PROXY = Counter(
    "gw_proxy_requests",
    "Catch-all proxy requests by service and upstream status class",
    ["service", "status"],  # 2xx | 4xx | 5xx | unreachable
)

# `reason` is what tells a misconfigured caller from someone trying tokens.
REFUSALS = Counter(
    "gw_refusals",
    "Requests refused before any work was done, by which control refused",
    ["reason"],  # auth | unknown_service | bad_attachment
)

# The same numbers provider.py accumulates, somewhere a deploy can read them.
MODEL_TOKENS = Counter(
    "gw_model_tokens",
    "Tokens billed by the router, by direction",
    ["direction"],  # prompt | output
)
