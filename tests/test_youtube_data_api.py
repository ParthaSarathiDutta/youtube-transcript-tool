"""Tests for YouTube Data API helpers and client error handling."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from discovery.youtube_data_api import (
    YouTubeApiError,
    YouTubeDataApiClient,
    parse_iso8601_duration_seconds,
    parse_rfc3339_utc,
)


def test_parse_rfc3339_utc_z_suffix():
    dt = parse_rfc3339_utc("2026-10-16T12:00:00Z")
    assert dt == datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def test_parse_iso8601_duration_seconds():
    assert parse_iso8601_duration_seconds("PT45S") == 45
    assert parse_iso8601_duration_seconds("PT1M") == 60
    assert parse_iso8601_duration_seconds("PT1H2M3S") == 3723


def test_injected_http_failure_surfaces_as_api_error():
    def failing_get(url: str, timeout: float):
        raise YouTubeApiError("YouTube API HTTP 403: quotaExceeded", status=403)

    client = YouTubeDataApiClient("test-key", http_get=failing_get)
    with pytest.raises(YouTubeApiError) as exc:
        client.resolve_channel_id_for_handle("aiDotEngineer")
    assert exc.value.status == 403


def test_timeout_raises_youtube_api_error():
    def timeout_get(url: str, timeout: float):
        raise TimeoutError("timed out")

    client = YouTubeDataApiClient("test-key", http_get=timeout_get)
    with pytest.raises(YouTubeApiError, match="timeout"):
        client.resolve_channel_id_for_handle("aiDotEngineer")
