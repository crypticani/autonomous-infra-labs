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

# The audit log is a safety control and has to outlive the container; a real deploy
# bind-mounts over this. Here so the image also works with no mount at all.
RUN mkdir -p /app/audit && chown -R appuser:appgroup /app

USER appuser

EXPOSE 7200

# --workers 1: approvals._proposals is in-process, so a second worker would answer
# "expired or unknown" to a valid Slack Approve.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7200", "--workers", "1"]
