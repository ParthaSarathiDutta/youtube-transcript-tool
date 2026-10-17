"""Reporting and recovery time windows (America/Chicago boundaries, UTC storage)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from discovery.policies import (
    DEFAULT_DIGEST_HOUR_LOCAL,
    DEFAULT_RECOVERY_LOOKBACK_DAYS,
    DEFAULT_REPORTING_LOOKBACK_HOURS,
)

CHICAGO = ZoneInfo("America/Chicago")

# Reporting window policy (explicit):
# - Digest anchor = most recent 7:00 America/Chicago not after run time.
# - Primary window = [anchor_end - lookback_hours, anchor_end) using a fixed
#   timedelta in UTC (default 24 hours), NOT "local clock 7am-to-7am elapsed".
# - On DST transition days the local wall-clock span may be 23h or 25h; the
#   pipeline intentionally uses a 24h UTC timedelta for predictable idempotency.
REPORTING_WINDOW_POLICY = "fixed_timedelta_from_chicago_anchor"


@dataclass(frozen=True)
class DiscoveryWindow:
    """Inclusive-exclusive UTC intervals for discovery classification."""

    as_of_utc: datetime
    anchor_end_utc: datetime
    reporting_start_utc: datetime
    reporting_end_utc: datetime
    recovery_start_utc: datetime
    delayed_start_utc: datetime
    delayed_end_utc: datetime

    def contains_reporting(self, published_at_utc: datetime) -> bool:
        return self.reporting_start_utc <= published_at_utc < self.reporting_end_utc

    def contains_recovery(self, published_at_utc: datetime) -> bool:
        return self.recovery_start_utc <= published_at_utc < self.reporting_start_utc

    def contains_delayed_gap(self, published_at_utc: datetime) -> bool:
        return self.delayed_start_utc <= published_at_utc < self.delayed_end_utc


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("Datetime must be timezone-aware (UTC recommended).")
    return dt.astimezone(timezone.utc)


def digest_anchor_end_utc(
    as_of: datetime,
    *,
    digest_hour_local: int = DEFAULT_DIGEST_HOUR_LOCAL,
    tz: ZoneInfo = CHICAGO,
) -> datetime:
    """
    Most recent scheduled digest anchor (digest_hour_local in ``tz``) that is
    not after ``as_of``. Late cron runs still use this anchor for the primary
    24h reporting window; videos after the anchor until ``as_of`` are classified
    separately so they are not permanently missed.
    """
    as_of = ensure_utc(as_of)
    local = as_of.astimezone(tz)
    candidate = local.replace(
        hour=digest_hour_local, minute=0, second=0, microsecond=0
    )
    if local < candidate:
        candidate -= timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def build_discovery_window(
    as_of: datetime,
    *,
    lookback_hours: float = DEFAULT_REPORTING_LOOKBACK_HOURS,
    recovery_lookback_days: int = DEFAULT_RECOVERY_LOOKBACK_DAYS,
    digest_hour_local: int = DEFAULT_DIGEST_HOUR_LOCAL,
    tz: ZoneInfo = CHICAGO,
) -> DiscoveryWindow:
    as_of_utc = ensure_utc(as_of)
    anchor_end = digest_anchor_end_utc(
        as_of_utc, digest_hour_local=digest_hour_local, tz=tz
    )
    reporting_start = anchor_end - timedelta(hours=lookback_hours)
    recovery_start = anchor_end - timedelta(days=recovery_lookback_days)
    return DiscoveryWindow(
        as_of_utc=as_of_utc,
        anchor_end_utc=anchor_end,
        reporting_start_utc=reporting_start,
        reporting_end_utc=anchor_end,
        recovery_start_utc=recovery_start,
        delayed_start_utc=anchor_end,
        delayed_end_utc=as_of_utc,
    )
