"""Shared test configuration."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_vision_retry_backoff():
    """Zero the vision unit-retry backoff so retry-path tests don't sleep.

    Production backs off 20s x attempt between retryable vision failures
    (throttle windows); tests exercising those paths stub the provider and
    must stay fast.
    """

    from services import catalogue_evidence_extraction as extraction

    previous = extraction._VISION_RETRY_BACKOFF_SECONDS
    extraction._VISION_RETRY_BACKOFF_SECONDS = 0.0
    yield
    extraction._VISION_RETRY_BACKOFF_SECONDS = previous
