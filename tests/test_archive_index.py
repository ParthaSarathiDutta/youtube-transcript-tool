"""Archive index merge preserves original timestamps."""

from __future__ import annotations

from datetime import datetime, timezone

from discovery.models import (
    DiscoveryCategory,
    DiscoveryRecord,
    DiscoveryStatus,
    VideoType,
)
from pipeline.archive_index import (
    TRANSCRIPT_SCOPE_PLAYLIST,
    TRANSCRIPT_SCOPE_SYNTHETIC_DATA,
    upsert_video_index_entry,
)
from pipeline.archive_store import video_archived_for_daily_digest
from transcript_service import TranscriptResult


def _record() -> DiscoveryRecord:
    discovered = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return DiscoveryRecord(
        video_id="vid12345678",
        channel_id="UCx",
        title="Talk",
        youtube_url="https://www.youtube.com/watch?v=vid12345678",
        published_at_utc=datetime(2020, 1, 1, tzinfo=timezone.utc),
        discovery_timestamp_utc=discovered,
        video_type=VideoType.STANDARD,
        status=DiscoveryStatus.DISCOVERED,
        discovery_category=DiscoveryCategory.MANUAL_VIDEO_ID,
    )


def test_upsert_preserves_publish_and_transcript_generated_at():
    index: dict = {
        "videos": {
            "vid12345678": {
                "youtube_published_at_utc": "2019-06-01T00:00:00+00:00",
                "published_at_utc": "2019-06-01T00:00:00+00:00",
                "transcript_generated_at_utc": "2025-01-01T12:00:00+00:00",
            }
        }
    }
    transcript = TranscriptResult(
        segments=[],
        language_code="en",
        is_generated=True,
        display_text="hello",
        provider="test",
    )
    upsert_video_index_entry(index, _record(), transcript, success=True)
    entry = index["videos"]["vid12345678"]
    assert entry["youtube_published_at_utc"] == "2019-06-01T00:00:00+00:00"
    assert entry["published_at_utc"] == "2019-06-01T00:00:00+00:00"
    assert entry["transcript_generated_at_utc"] == "2025-01-01T12:00:00+00:00"


def test_playlist_scope_does_not_mark_daily_transcript_success():
    index: dict = {"videos": {}}
    transcript = TranscriptResult(
        segments=[],
        language_code="en",
        is_generated=True,
        display_text="hello",
        provider="test",
    )
    upsert_video_index_entry(
        index,
        _record(),
        transcript,
        success=True,
        transcript_scope=TRANSCRIPT_SCOPE_PLAYLIST,
    )
    entry = index["videos"]["vid12345678"]
    assert entry.get("evals_playlist_transcript_status") == "success"
    assert entry.get("transcript_status") is None
    assert video_archived_for_daily_digest(index, "vid12345678") is False


def test_synthetic_playlist_scope_does_not_mark_daily_transcript_success():
    index: dict = {"videos": {}}
    transcript = TranscriptResult(
        segments=[],
        language_code="en",
        is_generated=True,
        display_text="hello",
        provider="test",
    )
    upsert_video_index_entry(
        index,
        _record(),
        transcript,
        success=True,
        transcript_scope=TRANSCRIPT_SCOPE_SYNTHETIC_DATA,
    )
    entry = index["videos"]["vid12345678"]
    assert entry.get("synthetic_data_playlist_transcript_status") == "success"
    assert entry.get("transcript_status") is None


def test_upsert_sets_transcript_generated_at_on_first_success():
    index: dict = {"videos": {}}
    transcript = TranscriptResult(
        segments=[],
        language_code="en",
        is_generated=True,
        display_text="hello",
        provider="test",
    )
    upsert_video_index_entry(index, _record(), transcript, success=True)
    entry = index["videos"]["vid12345678"]
    assert entry["transcript_generated_at_utc"]
    assert entry["youtube_published_at_utc"] == "2020-01-01T00:00:00+00:00"
