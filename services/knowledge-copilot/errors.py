"""Upstream failures, and which upstream failed.

UpstreamError lived in llm.py and retrieval.py imported it to report an *embedding*
failure, so a caller could not tell which outage it had. The subclass makes the
distinction available without changing what any existing handler catches.
"""


class UpstreamError(RuntimeError):
    """A service this one depends on failed."""

    def __init__(self, message: str, status: int, provider: str = "unknown") -> None:
        super().__init__(message)
        self.status = status
        self.provider = provider


class EmbeddingError(UpstreamError):
    """The embedding backend failed -- not the generator. Still an UpstreamError, so app.py's
    status mapping keeps working. The point is the label, not a new code path.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message, status, provider="embeddings")
