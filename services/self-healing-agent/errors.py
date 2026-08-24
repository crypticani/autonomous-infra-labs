"""Who failed, and whether anything failed at all.

app.py wants one `except UpstreamError` that maps any dependency's failure to a status
code, while the audit log and the metrics need to know *which* dependency.

All four are declared up front rather than appearing as each is needed -- adding to a
failure taxonomy a class at a time is how two subclasses end up meaning the same thing.

GuardrailViolation deliberately does NOT inherit from UpstreamError. Nothing upstream
failed: the agent refused. Collapsing the two would make a working guardrail look like
a broken cluster.
"""


class UpstreamError(RuntimeError):
    """A service this one depends on failed."""

    def __init__(self, message: str, status: int, provider: str = "unknown") -> None:
        super().__init__(message)
        self.status = status
        self.provider = provider


class AgentProviderError(UpstreamError):
    """The model backend failed -- not Kubernetes, not the copilot."""

    def __init__(self, message: str, status: int, provider: str) -> None:
        super().__init__(message, status, provider=provider)


class K8sError(UpstreamError):
    """The Kubernetes API server rejected a tool's request, or could not serve it."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message, status, provider="kubernetes")


class RunbookError(UpstreamError):
    """knowledge-copilot's /search-runbooks did not answer."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message, status, provider="knowledge-copilot")


class GuardrailViolation(RuntimeError):
    """The agent refused to do something. Nothing broke."""

    def __init__(self, message: str, guard: str) -> None:
        super().__init__(message)
        self.guard = guard
