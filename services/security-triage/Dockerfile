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

# Numeric uid, unlike the sibling images: `USER <name>` leaves a non-numeric user in the
# image config, which a pod with runAsNonRoot refuses to start. k8s/deployment.yaml pins
# the same 10001.
RUN addgroup --system --gid 10001 appgroup \
    && adduser --system --uid 10001 --ingroup appgroup appuser

COPY --from=builder /opt/venv /opt/venv

COPY . .

RUN chown -R appuser:appgroup /app

USER 10001

EXPOSE 7300

# --workers 1 for three reasons here: _runs holds every run's verdict, _starts holds the
# rate-limit buckets, and the Prometheus registry is per-process.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7300", "--workers", "1"]
