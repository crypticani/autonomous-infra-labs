"""The four services, described once.

Three consumers read this table and they must not disagree: the router prompt is generated
from `answers`, the needs-check reads `needs`, and the proxy reads `url()` and `token()`.
Writing a service's shape down twice is how a router starts recommending a service the
forwarding code cannot reach.

`answers` is prompt text, so it is written for a model rather than for a developer -- and
each entry names what its service needs, because the model's classification and the code's
needs-check have to agree about that or the two halves of a decline contradict each other.
"""

import json
import os
from dataclasses import dataclass
from typing import Callable

# Compose service names, not localhost: inside the gateway container `localhost` is the
# gateway, and three of the four siblings bind 127.0.0.1 on the host so
# host.docker.internal cannot reach them either. Only a bare `python app.py` wants ports
# on localhost, and that is what the env vars are for.
#
# ponytail: four env vars rather than one templated base. A per-service override is what a
# split deploy needs, and the four are already written down in .env.example anyway.


@dataclass(frozen=True)
class Backend:
    name: str
    # Goes into the router prompt verbatim.
    answers: str
    path: str
    url_env: str
    default_url: str
    # Empty for log-analyzer, which has no auth of its own.
    token_env: str
    # None means the question alone is enough. Anything else is the field name the caller
    # has to attach, and the string the needs-check reports.
    needs: str | None
    # What to tell a caller who did not attach it. Not derivable from `needs`: "raw_log"
    # does not tell anybody where to get one.
    hint: str
    body: Callable[[str, str], dict]

    def url(self) -> str:
        """Resolved per call, not frozen at import.

        The sibling services read their config once at import and report it on /health,
        which is right for a policy value somebody might have got wrong. A backend address
        is not policy -- it is the difference between a test pointing at a local stub and a
        container pointing at compose DNS, and an import-time read makes the first of those
        need `object.__setattr__` on a frozen dataclass.
        """
        return os.getenv(self.url_env) or self.default_url

    def token(self) -> str:
        """The backend's own bearer token, or empty if it has none.

        Split on comma and take the first: security-triage's ST_API_TOKENS is plural (one
        per onboarded repo) while the others are singular. A singular value contains no
        comma, so one code path covers both and there is no per-backend flag saying which
        shape to expect.
        """
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
        # Rewritten 2026-08-26 from the first measured run, where this service was the
        # catalogue's attractor: picked 8 times out of 24 and wrong on 3 of them, and the
        # model's stated reason for the worst miss was the bare phrase "operational
        # question" -- a quotation of this very description. Three things had to go:
        # "operational questions", the broadest phrase in the whole catalogue; "what an
        # alert means", which collides head-on with both log-analyzer and the agent; and
        # "Needs nothing but the question", which reads as "pick me when unsure". What
        # replaces them is narrow, and the last sentence pushes back rather than inviting.
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
        # No `k`: the copilot has its own default and duplicating it here gives it a
        # second place to drift from.
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
        # The attachment *is* the request body: scan.sh already emits exactly the envelope
        # POST /triage takes, repo and all, so re-assembling one here would only be a
        # chance to assemble it differently.
        body=lambda question, attachment: json.loads(attachment),
    ),
)

BY_NAME: dict[str, Backend] = {b.name: b for b in BACKENDS}

# The enum the router's schema constrains `service` to. Built from the table so a fifth
# backend cannot be added without the model being allowed to name it.
NAMES: tuple[str, ...] = tuple(BY_NAME)


def catalogue() -> str:
    """The service list, as the router prompt sees it."""
    return "\n".join(f"- {b.name}: {b.answers}" for b in BACKENDS)
