"""Reproduce the deliberate retry-delay failure, without any Sales Buddy imports."""

from upgrade import retry_delay


def test_retry_backoff_is_capped() -> None:
    """A fourth retry must not wait longer than the sixty-second recovery limit."""
    assert retry_delay(4) == 60
