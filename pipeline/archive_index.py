"""Archive index field conventions and merge rules."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from discovery.models import DiscoveryRecord
from pipeline.archive_paths import build_video_folder_name
from transcript_service import TranscriptResult

# Phase 2: transcript_generated_at_utc is set once when a transcript is first
# stored successfully. Later syncs must not overwrite youtube_published_at_utc or
# transcript_generated_at_utc (use setdefault / preserve helpers below).

TRANSCRIPT_SCOPE_DAILY = "daily_digest"
TRANSCRIPT_SCOPE_PLAYLIST = "evals_playlist"


def upsert_video_index_entry(
    index: dict[str, Any],
    record: DiscoveryRecord,
    transcript: TranscriptResult,
    *,
    success: bool,
    transcript_scope: Literal["daily_digest", "evals_playlist"] = TRANSCRIPT_SCOPE_DAILY,
) -> None:
    """Merge index fields without clobbering original publish/generation timestamps."""
    videos = index.setdefault("videos", {})
    entry = videos.setdefault(record.video_id, {})
    now = datetime.now(timezone.utc).isoformat()

    entry["title"] = record.title
    entry["transcript_provider"] = transcript.provider
    entry["updated_at_utc"] = now
    entry.setdefault(
        "archive_folder",
        build_video_folder_name(record.title, record.video_id),
    )

    if transcript_scope == TRANSCRIPT_SCOPE_PLAYLIST:
        entry["evals_playlist_transcript_status"] = "success" if success else "failed"
        if success:
            entry.setdefault("evals_playlist_transcript_generated_at_utc", now)
        return

    entry["transcript_scope"] = TRANSCRIPT_SCOPE_DAILY
    entry["transcript_status"] = "success" if success else "failed"
    youtube_published = record.published_at_utc.isoformat()
    entry.setdefault("youtube_published_at_utc", youtube_published)
    entry.setdefault("published_at_utc", youtube_published)
    if success:
        entry.setdefault("transcript_generated_at_utc", now)
