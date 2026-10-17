"""Tests for video discovery service with mocked API client."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from discovery.models import DiscoveryCategory, DiscoveryStatus, VideoType
from discovery.service import VideoDiscoveryService, categorize_published_at
from discovery.window import build_discovery_window
from discovery.youtube_data_api import ApiVideoDetails, YouTubeDataApiClient

UTC = timezone.utc
CHANNEL_ID = "UC_test_channel"
UPLOADS_ID = "UU_uploads_playlist"


def _video(
    video_id: str,
    published: datetime,
    *,
    duration: int = 600,
    live: str = "none",
    privacy: str = "public",
    upload: str = "processed",
    title: str | None = None,
) -> ApiVideoDetails:
    return ApiVideoDetails(
        video_id=video_id,
        channel_id=CHANNEL_ID,
        title=title or f"Title {video_id}",
        published_at_utc=published,
        privacy_status=privacy,
        upload_status=upload,
        live_broadcast_content=live,
        duration_seconds=duration,
    )


def _mock_client(
    playlist_entries: list[tuple[str, datetime | None]],
    videos: dict[str, ApiVideoDetails],
) -> YouTubeDataApiClient:
    client = MagicMock(spec=YouTubeDataApiClient)
    client.resolve_channel_id_for_handle.return_value = CHANNEL_ID
    client.get_uploads_playlist_id.return_value = UPLOADS_ID
    client.iter_recent_upload_playlist_entries.return_value = iter(playlist_entries)
    client.get_videos_details.side_effect = lambda ids: {
        vid: videos[vid] for vid in ids if vid in videos
    }
    return client


def _as_of() -> datetime:
    # Oct 16 2026 12:00 UTC → 7 AM CDT anchor on Oct 16
    return datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def test_zero_videos_in_window():
    as_of = _as_of()
    window = build_discovery_window(as_of)
    old = window.recovery_start_utc - timedelta(days=30)
    client = _mock_client([], {})
    service = VideoDiscoveryService(client)
    _, records = service.discover_for_handle("aiDotEngineer", as_of=as_of)
    assert records == []


def test_one_video_in_reporting_window():
    as_of = _as_of()
    window = build_discovery_window(as_of)
    published = window.reporting_start_utc + timedelta(hours=1)
    vid = "abc12345678"
    client = _mock_client([(vid, published)], {vid: _video(vid, published)})
    service = VideoDiscoveryService(client)
    _, records = service.discover_for_handle("aiDotEngineer", as_of=as_of)
    assert len(records) == 1
    assert records[0].video_id == vid
    assert records[0].discovery_category == DiscoveryCategory.REPORTING_WINDOW
    assert records[0].status == DiscoveryStatus.DISCOVERED


def test_three_videos_in_one_window():
    as_of = _as_of()
    window = build_discovery_window(as_of)
    base = window.reporting_start_utc + timedelta(hours=1)
    ids = ["vid00000001", "vid00000002", "vid00000003"]
    playlist = [(i, base + timedelta(hours=h)) for h, i in enumerate(ids)]
    videos = {i: _video(i, base + timedelta(hours=h)) for h, i in enumerate(ids)}
    client = _mock_client(playlist, videos)
    service = VideoDiscoveryService(client)
    _, records = service.discover_for_handle("aiDotEngineer", as_of=as_of)
    assert len(records) == 3


def test_video_outside_reporting_and_recovery_excluded():
    as_of = _as_of()
    window = build_discovery_window(as_of)
    published = window.recovery_start_utc - timedelta(hours=1)
    vid = "old00000001"
    client = _mock_client([(vid, published)], {vid: _video(vid, published)})
    service = VideoDiscoveryService(client)
    _, records = service.discover_for_handle("aiDotEngineer", as_of=as_of)
    assert records == []


def test_recovery_candidate_outside_24h_but_inside_7d():
    as_of = _as_of()
    window = build_discovery_window(as_of)
    published = window.reporting_start_utc - timedelta(days=2)
    vid = "rec00000001"
    client = _mock_client([(vid, published)], {vid: _video(vid, published)})
    service = VideoDiscoveryService(client)
    _, records = service.discover_for_handle("aiDotEngineer", as_of=as_of)
    assert len(records) == 1
    assert records[0].discovery_category == DiscoveryCategory.RECOVERY


def test_delayed_gap_since_anchor():
    as_of = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)  # 9 AM CDT
    window = build_discovery_window(as_of)
    published = window.anchor_end_utc + timedelta(minutes=30)
    vid = "late0000001"
    client = _mock_client([(vid, published)], {vid: _video(vid, published)})
    service = VideoDiscoveryService(client)
    _, records = service.discover_for_handle("aiDotEngineer", as_of=as_of)
    assert len(records) == 1
    assert records[0].discovery_category == DiscoveryCategory.DELAYED_SINCE_ANCHOR


def test_duplicate_playlist_ids_deduped():
    as_of = _as_of()
    window = build_discovery_window(as_of)
    published = window.reporting_start_utc + timedelta(hours=2)
    vid = "dup00000001"
    client = _mock_client(
        [(vid, published), (vid, published)],
        {vid: _video(vid, published)},
    )
    service = VideoDiscoveryService(client)
    _, records = service.discover_for_handle("aiDotEngineer", as_of=as_of)
    assert len(records) == 1


def test_pagination_stop_before_recovery_start():
    as_of = _as_of()
    window = build_discovery_window(as_of)

    def fake_iter(uploads_playlist_id, *, stop_before_utc, max_pages=100):
        assert stop_before_utc == window.recovery_start_utc
        yield "new00000001", window.reporting_start_utc + timedelta(hours=1)

    client = MagicMock(spec=YouTubeDataApiClient)
    client.resolve_channel_id_for_handle.return_value = CHANNEL_ID
    client.get_uploads_playlist_id.return_value = UPLOADS_ID
    client.iter_recent_upload_playlist_entries.side_effect = fake_iter
    client.get_videos_details.return_value = {
        "new00000001": _video(
            "new00000001",
            window.reporting_start_utc + timedelta(hours=1),
        )
    }
    service = VideoDiscoveryService(client)
    service.discover_for_handle("aiDotEngineer", as_of=as_of)
    client.iter_recent_upload_playlist_entries.assert_called_once()


def test_unavailable_video_with_playlist_timestamp():
    as_of = _as_of()
    window = build_discovery_window(as_of)
    published = window.reporting_start_utc + timedelta(hours=1)
    vid = "del00000001"
    client = _mock_client([(vid, published)], {})
    service = VideoDiscoveryService(client)
    _, records = service.discover_for_handle("aiDotEngineer", as_of=as_of)
    assert len(records) == 1
    assert records[0].status == DiscoveryStatus.UNAVAILABLE


def test_private_video_marked_private():
    as_of = _as_of()
    window = build_discovery_window(as_of)
    published = window.reporting_start_utc + timedelta(hours=1)
    vid = "prv00000001"
    client = _mock_client(
        [(vid, published)],
        {vid: _video(vid, published, privacy="private")},
    )
    service = VideoDiscoveryService(client)
    _, records = service.discover_for_handle("aiDotEngineer", as_of=as_of)
    assert records[0].status == DiscoveryStatus.PRIVATE


def test_short_duration_does_not_auto_classify_as_short():
    as_of = _as_of()
    window = build_discovery_window(as_of)
    published = window.reporting_start_utc + timedelta(hours=1)
    vid = "sht00000001"
    client = _mock_client(
        [(vid, published)],
        {vid: _video(vid, published, duration=30)},
    )
    service = VideoDiscoveryService(client)
    _, records = service.discover_for_handle("aiDotEngineer", as_of=as_of)
    assert records[0].video_type == VideoType.STANDARD


def test_manual_video_id_mode():
    as_of = _as_of()
    window = build_discovery_window(as_of)
    published = window.recovery_start_utc - timedelta(days=30)
    vid = "manual00001"
    client = _mock_client([], {vid: _video(vid, published)})
    service = VideoDiscoveryService(client)
    _, record = service.discover_single_video(vid, as_of=as_of)
    assert record is not None
    assert record.discovery_category == DiscoveryCategory.MANUAL_VIDEO_ID
