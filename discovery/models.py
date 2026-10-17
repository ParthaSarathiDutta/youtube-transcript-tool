"""Data models for video discovery results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class VideoType(str, Enum):
    """Best-effort classification from YouTube Data API fields."""

    STANDARD = "standard"
    SHORT = "short"
    LIVE_STREAM = "live_stream"
    UPCOMING = "upcoming"
    UNKNOWN = "unknown"


class DiscoveryCategory(str, Enum):
    """Why this video was selected in this run."""

    REPORTING_WINDOW = "reporting_window"
    RECOVERY = "recovery"
    DELAYED_SINCE_ANCHOR = "delayed_since_anchor"
    MANUAL_VIDEO_ID = "manual_video_id"


class DiscoveryStatus(str, Enum):
    """Processing readiness after discovery."""

    DISCOVERED = "discovered"
    UNAVAILABLE = "unavailable"
    PRIVATE = "private"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class DiscoveryRecord:
    video_id: str
    channel_id: str
    title: str
    youtube_url: str
    published_at_utc: datetime
    discovery_timestamp_utc: datetime
    video_type: VideoType
    status: DiscoveryStatus
    discovery_category: DiscoveryCategory

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "channel_id": self.channel_id,
            "title": self.title,
            "youtube_url": self.youtube_url,
            "published_at_utc": self.published_at_utc.isoformat(),
            "discovery_timestamp_utc": self.discovery_timestamp_utc.isoformat(),
            "video_type": self.video_type.value,
            "status": self.status.value,
            "discovery_category": self.discovery_category.value,
        }
