"""Deliberately broken retry helper for the isolated Copilot experiment."""


def retry_delay(attempt: int) -> int:
    """Return exponential backoff in seconds, capped at sixty seconds."""
    if attempt < 0:
        raise ValueError('Attempt must not be negative')
    return 5 * (2 ** attempt)
