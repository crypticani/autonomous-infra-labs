"""One error type for anything the model backend gets wrong.

Same shape as knowledge-copilot's UpstreamError and self-healing-agent's
AgentProviderError -- a status the caller should return, and which provider to go look
at. Covers both transport failures (timeout, unreachable, rejected request) and a model
that answered but produced something triage.py can't use (malformed JSON, a schema
violation): either way, the caller could not get a usable triage out of this call.
"""


class TriageProviderError(RuntimeError):
    def __init__(self, message: str, status: int, provider: str = "unknown") -> None:
        super().__init__(message)
        self.status = status
        self.provider = provider
