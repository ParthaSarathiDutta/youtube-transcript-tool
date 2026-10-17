"""Documented inclusion policies for discovery."""

from __future__ import annotations

# YouTube Shorts can be up to ~3 minutes and are not reliably identified by
# duration alone. We do not auto-label SHORT from duration; use --exclude-shorts
# only when you add an explicit Shorts signal (future: API / page metadata).
SHORTS_MAX_DURATION_SECONDS = 180  # documented upper bound only; not used for auto-tagging

# Scheduled digest anchor in America/Chicago (7:00 AM daily target).
DEFAULT_DIGEST_HOUR_LOCAL = 7
DEFAULT_REPORTING_LOOKBACK_HOURS = 24
DEFAULT_RECOVERY_LOOKBACK_DAYS = 7
