"""Upstream failures, and which upstream failed.

UpstreamError lived in llm.py, and retrieval.py imported it to report a failure of the
*embedding* backend. A caller catching it could not tell whether generation failed or
embedding did -- two different outages with two different fixes, arriving as the same
exception. The subclass makes the distinction available without changing what any
existing handler catches.
"""


class UpstreamError(RuntimeError):
    """A service this one depends on failed.

    `status` is what the caller should return; `provider` is who to go and look at.
    """

    def __init__(self, message: str, status: int, provider: str = "unknown") -> None:
        super().__init__(message)
        self.status = status
        self.provider = provider


class EmbeddingError(UpstreamError):
    """The embedding backend failed -- not the generator.

    Still an UpstreamError, so app.py's `except UpstreamError` and its status mapping
    keep working untouched. The point is the label, not a new code path.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message, status, provider="embeddings")
