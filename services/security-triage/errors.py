"""One error type for anything the model backend gets wrong."""


class TriageProviderError(RuntimeError):
    def __init__(self, message: str, status: int, provider: str = "unknown") -> None:
        super().__init__(message)
        self.status = status
        self.provider = provider
