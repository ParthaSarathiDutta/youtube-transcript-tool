"""Tests for America/Chicago digest window boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from discovery.window import CHICAGO, build_discovery_window, digest_anchor_end_utc

UTC = timezone.utc


def test_digest_anchor_at_scheduled_time():
    # 2026-10-16 07:05 AM Chicago (CDT, UTC-5)
    as_of = datetime(2026, 10, 16, 12, 5, tzinfo=UTC)
    anchor = digest_anchor_end_utc(as_of).astimezone(CHICAGO)
    assert anchor == datetime(2026, 10, 16, 7, 0, tzinfo=CHICAGO)


def test_delayed_run_uses_same_anchor_end():
    # Run at 9 AM Chicago; anchor still 7 AM same day
    as_of = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)  # 9 AM CDT
    window = build_discovery_window(as_of, lookback_hours=24, recovery_lookback_days=7)
    assert window.anchor_end_utc.astimezone(CHICAGO) == datetime(
        2026, 10, 16, 7, 0, tzinfo=CHICAGO
    )
    assert window.delayed_start_utc == window.anchor_end_utc
    assert window.delayed_end_utc == as_of


def test_before_anchor_uses_previous_day():
    # 6:30 AM Chicago on Oct 16 → anchor Oct 15 7 AM
    as_of = datetime(2026, 10, 16, 11, 30, tzinfo=UTC)
    anchor = digest_anchor_end_utc(as_of).astimezone(CHICAGO)
    assert anchor == datetime(2026, 10, 15, 7, 0, tzinfo=CHICAGO)


def test_dst_spring_forward_boundary():
    # US spring forward 2026-03-08 2 AM → 3 AM in Chicago
    as_of = datetime(2026, 3, 8, 14, 0, tzinfo=UTC)  # 9 AM CDT after transition
    window = build_discovery_window(as_of, lookback_hours=24, recovery_lookback_days=7)
    reporting_hours = (
        window.reporting_end_utc - window.reporting_start_utc
    ).total_seconds() / 3600
    assert reporting_hours == 24


def test_dst_fall_back_boundary():
    as_of = datetime(2026, 11, 1, 13, 0, tzinfo=UTC)
    window = build_discovery_window(as_of, lookback_hours=24, recovery_lookback_days=7)
    reporting_hours = (
        window.reporting_end_utc - window.reporting_start_utc
    ).total_seconds() / 3600
    assert reporting_hours == 24


def test_recovery_window_precedes_reporting_window():
    as_of = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    window = build_discovery_window(as_of, lookback_hours=24, recovery_lookback_days=7)
    assert window.recovery_start_utc < window.reporting_start_utc
    assert window.reporting_start_utc == window.anchor_end_utc - timedelta(hours=24)
