"""The four services, described once.

`answers` is prompt text -- the router's catalogue is generated from it, so edits here
change routing behaviour. See Readme.md before rewording one.
"""

import json
import os
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Backend:
    name: str
    answers: str
    path: str
    url_env: str
    # Compose service names: inside the container `localhost` is the gateway.
    default_url: str
    # Empty for log-analyzer, which has no auth of its own.
    token_env: str
    # None means the question alone is enough; otherwise the field the caller must attach.
    needs: str | None
    # What to tell a caller who did not attach it; not derivable from `needs`.
    hint: str
    body: Callable[[str, str], dict]

    def url(self) -> str:
        """Per call, not at import, so a test can point this at a stub."""
        return os.getenv(self.url_env) or self.default_url

    def token(self) -> str:
        """First of a comma-separated value, so plural ST_API_TOKENS and the singular
        others take one code path."""
        if not self.token_env:
            return ""
        raw = os.getenv(self.token_env, "")
        return next((t.strip() for t in raw.split(",") if t.strip()), "")

    def headers(self) -> dict[str, str]:
        token = self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}


BACKENDS: tuple[Backend, ...] = (
    Backend(
        name="knowledge-copilot",
        # Narrowed 2026-08-26: the broad first version made this the catalogue's attractor.
        answers=(
            "Looks up what this team has already written down: a documented procedure, an "
            "agreed escalation path, a definition or threshold this team has settled on, a "
            "postmortem of something that happened before. It searches documents and "
            "knows nothing whatever about the running system -- it cannot see logs, the "
            "cluster, or scan results. If the answer is not already written in a runbook, "
            "this is the wrong service."
        ),
        path="/ask-runbook",
        url_env="GW_KNOWLEDGE_COPILOT_URL",
        default_url="http://knowledge-copilot:7100",
        token_env="KC_API_TOKEN",
        needs=None,
        hint="",
        # No `k`: the copilot's own default is the only place that number should live.
        body=lambda question, attachment: {"question": question},
    ),
    Backend(
        name="log-analyzer",
        answers=(
            "Reads raw log text and returns a structured diagnosis: severity, error type, "
            "root cause and a suggested fix. It analyses log text it is handed and cannot "
            "fetch, search or tail logs itself, so the log must be supplied."
        ),
        path="/analyze-log",
        url_env="GW_LOG_ANALYZER_URL",
        default_url="http://log-analyzer:7000",
        token_env="",
        needs="raw_log",
        hint="attach the log text itself as `attachment`",
        body=lambda question, attachment: {"raw_log": attachment},
    ),
    Backend(
        name="self-healing-agent",
        answers=(
            "Diagnoses a firing Prometheus or Alertmanager alert against a live Kubernetes "
            "cluster, gathering evidence with read-only tools, and proposes one "
            "remediation for a human to approve. Needs the alert as JSON; a description "
            "of an alert is not an alert."
        ),
        path="/diagnose",
        url_env="GW_SELF_HEALING_AGENT_URL",
        default_url="http://self-healing-agent:7200",
        token_env="SHA_API_TOKEN",
        needs="alert",
        hint="attach the Alertmanager alert as a JSON object in `attachment`",
        body=lambda question, attachment: {"alert": json.loads(attachment)},
    ),
    Backend(
        name="security-triage",
        answers=(
            "Triages security scanner findings -- Trivy, Bandit, Gitleaks, Checkov -- into "
            "one risk score, a CI pass or fail verdict, and fix diffs. It reasons about "
            "scanner output it is given and does not run the scanners, so the scan results "
            "must be supplied."
        ),
        path="/triage",
        url_env="GW_SECURITY_TRIAGE_URL",
        default_url="http://security-triage:7300",
        token_env="ST_API_TOKENS",
        needs="scan envelope",
        hint="attach `scan.sh`'s output envelope as JSON in `attachment`",
        # scan.sh already emits exactly what POST /triage takes.
        body=lambda question, attachment: json.loads(attachment),
    ),
)

BY_NAME: dict[str, Backend] = {b.name: b for b in BACKENDS}

# What router.py constrains the model's `service` field to.
NAMES: tuple[str, ...] = tuple(BY_NAME)


def catalogue() -> str:
    return "\n".join(f"- {b.name}: {b.answers}" for b in BACKENDS)
