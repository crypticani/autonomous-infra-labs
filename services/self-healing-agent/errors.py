"""Who failed, and whether anything failed at all.

Same split as knowledge-copilot's errors.py, for the same reason: app.py wants one
`except UpstreamError` that maps any dependency's failure to a status code, while the audit
log and the metrics need to know *which* dependency. The raiser knows; let it say so.

All four are declared now, on day one, rather than appearing as each day needs one. This is
the service's failure taxonomy -- adding to it a class at a time across a week is how two
subclasses end up meaning the same thing.

GuardrailViolation deliberately does NOT inherit from UpstreamError. Nothing upstream failed:
the agent looked at a request and refused it. That is a decision to report, not an outage to
retry, and collapsing the two would make a working guardrail look like a broken cluster.
"""


class UpstreamError(RuntimeError):
    """A service this one depends on failed.

    `status` is what the caller should return; `provider` is who to go and look at.
    """

    def __init__(self, message: str, status: int, provider: str = "unknown") -> None:
        super().__init__(message)
        self.status = status
        self.provider = provider


class AgentProviderError(UpstreamError):
    """The model backend failed -- not Kubernetes, not the copilot."""

    def __init__(self, message: str, status: int, provider: str) -> None:
        super().__init__(message, status, provider=provider)


class K8sError(UpstreamError):
    """The Kubernetes API server rejected a tool's request, or could not serve it.

    Carries the API server's own status through: a 403 here means the RoleBinding is too
    narrow, and a 404 means the pod is already gone. Those need different responses, and
    flattening both to 502 loses the only useful thing about the failure.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message, status, provider="kubernetes")


class RunbookError(UpstreamError):
    """knowledge-copilot's /search-runbooks did not answer."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message, status, provider="knowledge-copilot")


class GuardrailViolation(RuntimeError):
    """The agent refused to do something. Nothing broke.

    `guard` names which rule refused, because that is the label
    sha_guardrail_blocks_total needs and the sentence the human in Slack needs to read.
    """

    def __init__(self, message: str, guard: str) -> None:
        super().__init__(message)
        self.guard = guard
