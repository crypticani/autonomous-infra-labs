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

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

COPY --from=builder /opt/venv /opt/venv

COPY . .

# The audit log is a safety control, not a cache: it has to survive the container that
# wrote it. This directory exists so the image also works with no mount at all, but a
# real deploy bind-mounts over it -- an append-only record that dies with the container
# cannot answer "who approved that restart" a week later.
RUN mkdir -p /app/audit && chown -R appuser:appgroup /app

USER appuser

EXPOSE 7200

# --workers 1 is load-bearing here, for a different reason than the copilot's.
# approvals._proposals is in-process memory: with two workers, Slack's click can land
# on the worker that does not hold the proposal, and a valid Approve would come back
# "expired or unknown". Day 18's state machine is single-process by construction.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7200", "--workers", "1"]
