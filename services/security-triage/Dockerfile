# Stage 1: Build
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends gcc build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Explicit numeric uid, unlike the other three images, and k8s/deployment.yaml is why.
# `adduser --system` picks whatever is free in 100-999, and USER by *name* leaves the
# image config with a non-numeric user -- which a pod running `runAsNonRoot: true`
# refuses to start, because the kubelet cannot verify a name is not root. Pinning it here
# means the manifest's runAsUser and the image agree by construction rather than by
# whatever the base image had spare on build day.
RUN addgroup --system --gid 10001 appgroup \
    && adduser --system --uid 10001 --ingroup appgroup appuser

COPY --from=builder /opt/venv /opt/venv

COPY . .

RUN chown -R appuser:appgroup /app

USER 10001

# No bind mount and no writable directory, unlike the agent's /app/audit: run records
# live in a process-local dict by design (see GET /triage/{id}) and there is nothing here
# that has to outlive the container. A restart mid-run strands a polling CI job on a 404,
# which is the loud version of a failure that would otherwise be silent.

EXPOSE 7300

# No HEALTHCHECK, matching the other three images: compose defines one per service, and
# this repo's own scan flags DS-0026 on all three -- a finding eval_set.json asserts is
# noise. Adding one here alone would mean arguing both sides of the same finding in the
# same commit.

# --workers 1 is load-bearing for the fourth time in this repo, and here it is three
# things at once: `_runs` holds every run's verdict in process memory, `_starts` holds
# the per-token rate-limit buckets, and prometheus_client's default registry is
# per-process -- so a second worker would serve 404s for runs it never saw, allow double
# the rate limit, and export half the metrics on each scrape.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7300", "--workers", "1"]
