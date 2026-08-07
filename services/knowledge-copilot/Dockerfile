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

# chroma_data is created here so the image also works with no mount at all -- the
# directory has to exist and be writable before Chroma opens a PersistentClient.
# Compose bind-mounts the host's chroma_data over it and overrides `user:` to the
# host owner, because a bind mount keeps the host uid rather than the image's.
RUN mkdir -p /app/chroma_data && chown -R appuser:appgroup /app

USER appuser

EXPOSE 7100

# --workers 1: two workers means two alert-sync loops racing on the same writes.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7100", "--workers", "1"]
