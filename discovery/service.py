"""Discover channel uploads for the daily reporting and recovery windows."""

from __future__ import annotations

from datetime import datetime, timezone

from discovery.models import (
    DiscoveryCategory,
    DiscoveryRecord,
    DiscoveryStatus,
    VideoType,
)
from discovery.window import DiscoveryWindow, build_discovery_window
from discovery.youtube_data_api import ApiVideoDetails, YouTubeDataApiClient
from youtube_utils import build_video_url


class VideoDiscoveryService:
    def __init__(self, api_client: YouTubeDataApiClient):
        self._api = api_client

    def discover_for_handle(
        self,
        handle: str,
        *,
        as_of: datetime | None = None,
        lookback_hours: float = 24,
        recovery_lookback_days: int = 7,
        exclude_shorts: bool = False,
        include_outside_window: bool = False,
    ) -> tuple[DiscoveryWindow, list[DiscoveryRecord]]:
        as_of_utc = as_of or datetime.now(timezone.utc)
        window = build_discovery_window(
            as_of_utc,
            lookback_hours=lookback_hours,
            recovery_lookback_days=recovery_lookback_days,
        )
        discovery_ts = datetime.now(timezone.utc)
        channel_id = self._api.resolve_channel_id_for_handle(handle)
        uploads_id = self._api.get_uploads_playlist_id(channel_id)

        candidates: list[tuple[str, datetime | None]] = []
        seen: set[str] = set()
        for video_id, playlist_published in self._api.iter_recent_upload_playlist_entries(
            uploads_id,
            stop_before_utc=window.recovery_start_utc,
        ):
            if video_id in seen:
                continue
            seen.add(video_id)
            candidates.append((video_id, playlist_published))

        candidate_ids = [video_id for video_id, _ in candidates]
        details_map = self._api.get_videos_details(candidate_ids)
        records: list[DiscoveryRecord] = []

        for video_id, playlist_published in candidates:
            details = details_map.get(video_id)
            record = self._build_record(
                details,
                video_id=video_id,
                playlist_published_at_utc=playlist_published,
                fallback_channel_id=channel_id,
                window=window,
                discovery_timestamp_utc=discovery_ts,
                exclude_shorts=exclude_shorts,
                include_outside_window=include_outside_window,
                discovery_category=None,
            )
            if record is None:
                continue
            records.append(record)

        return window, records

    def discover_single_video(
        self,
        video_id: str,
        *,
        as_of: datetime | None = None,
        lookback_hours: float = 24,
        recovery_lookback_days: int = 7,
        exclude_shorts: bool = False,
    ) -> tuple[DiscoveryWindow, DiscoveryRecord | None]:
        as_of_utc = as_of or datetime.now(timezone.utc)
        window = build_discovery_window(
            as_of_utc,
            lookback_hours=lookback_hours,
            recovery_lookback_days=recovery_lookback_days,
        )
        discovery_ts = datetime.now(timezone.utc)
        details_map = self._api.get_videos_details([video_id])
        details = details_map.get(video_id)
        record = self._build_record(
            details,
            video_id=video_id,
            fallback_channel_id="",
            window=window,
            discovery_timestamp_utc=discovery_ts,
            exclude_shorts=exclude_shorts,
            include_outside_window=True,
            discovery_category=DiscoveryCategory.MANUAL_VIDEO_ID,
        )
        return window, record

    def _build_record(
        self,
        details: ApiVideoDetails | None,
        *,
        video_id: str,
        playlist_published_at_utc: datetime | None = None,
        fallback_channel_id: str,
        window: DiscoveryWindow,
        discovery_timestamp_utc: datetime,
        exclude_shorts: bool,
        include_outside_window: bool,
        discovery_category: DiscoveryCategory | None,
    ) -> DiscoveryRecord | None:
        if details is None:
            if playlist_published_at_utc is None:
                return None
            category = discovery_category or categorize_published_at(
                playlist_published_at_utc, window
            )
            if not include_outside_window and category is None:
                return None
            if category is None:
                category = DiscoveryCategory.MANUAL_VIDEO_ID
            return DiscoveryRecord(
                video_id=video_id,
                channel_id=fallback_channel_id,
                title=video_id,
                youtube_url=build_video_url(video_id),
                published_at_utc=playlist_published_at_utc,
                discovery_timestamp_utc=discovery_timestamp_utc,
                video_type=VideoType.UNKNOWN,
                status=DiscoveryStatus.UNAVAILABLE,
                discovery_category=category,
            )

        if details.privacy_status == "private":
            status = DiscoveryStatus.PRIVATE
        elif details.upload_status == "deleted":
            status = DiscoveryStatus.UNAVAILABLE
        else:
            status = DiscoveryStatus.DISCOVERED

        video_type = classify_video_type(details)
        if exclude_shorts and video_type == VideoType.SHORT:
            return None

        category = discovery_category or categorize_published_at(details.published_at_utc, window)
        if not include_outside_window and category is None:
            return None

        if category is None:
            category = DiscoveryCategory.MANUAL_VIDEO_ID

        return DiscoveryRecord(
            video_id=details.video_id,
            channel_id=details.channel_id or fallback_channel_id,
            title=details.title,
            youtube_url=build_video_url(details.video_id),
            published_at_utc=details.published_at_utc,
            discovery_timestamp_utc=discovery_timestamp_utc,
            video_type=video_type,
            status=status,
            discovery_category=category,
        )


def classify_video_type(details: ApiVideoDetails) -> VideoType:
    live = (details.live_broadcast_content or "none").lower()
    if live == "live":
        return VideoType.LIVE_STREAM
    if live == "upcoming":
        return VideoType.UPCOMING
    # Duration does not reliably distinguish Shorts (see discovery/policies.py).
    return VideoType.STANDARD


def categorize_published_at(
    published_at_utc: datetime,
    window: DiscoveryWindow,
) -> DiscoveryCategory | None:
    if window.contains_reporting(published_at_utc):
        return DiscoveryCategory.REPORTING_WINDOW
    if window.contains_delayed_gap(published_at_utc):
        return DiscoveryCategory.DELAYED_SINCE_ANCHOR
    if window.contains_recovery(published_at_utc):
        return DiscoveryCategory.RECOVERY
    return None
