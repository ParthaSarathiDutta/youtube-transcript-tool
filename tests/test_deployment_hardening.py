"""Deployment: archive hydration, git redaction, email retry behavior."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from discovery.models import (
    DiscoveryCategory,
    DiscoveryRecord,
    DiscoveryStatus,
    VideoType,
)
from pipeline.archive_store import load_hydrated_video
from pipeline.daily_run import run_daily_digest
from pipeline.config import PipelineConfig
from pipeline.git_archive import sanitize_git_message


def test_sanitize_git_message_redacts_tokens():
    raw = "fatal: https://x-access-token:ghp_secret@github.com failed"
    assert "ghp_secret" not in sanitize_git_message(raw)
    assert "***" in sanitize_git_message(raw)


def test_hydrate_archived_video_for_email_retry(tmp_path: Path):
    videos_dir = tmp_path / "videos"
    video_dir = videos_dir / "vid12345678"
    video_dir.mkdir(parents=True)
    (video_dir / "metadata.json").write_text(
        json.dumps(
            {
                "video_id": "vid12345678",
                "title": "Hydrated Title",
                "youtube_url": "https://www.youtube.com/watch?v=vid12345678",
                "published_at_utc": "2026-10-16T12:00:00+00:00",
                "transcript_status": "success",
                "transcript_provider": "youtube_transcript_api",
                "caption_language": "en",
                "captions_generated": True,
            }
        ),
        encoding="utf-8",
    )
    (video_dir / "transcript.md").write_text(
        "# Hydrated Title\n\nURL: https://youtu.be/x\n\nReal transcript body here.",
        encoding="utf-8",
    )
    (video_dir / "summary.md").write_text("## Main concept\nCached summary.", encoding="utf-8")

    hydrated = load_hydrated_video(videos_dir, "vid12345678")
    assert hydrated is not None
    assert "Real transcript body" in hydrated.doc_item.transcript.display_text
    assert "Cached summary" in hydrated.email_section


def test_email_retry_hydrates_without_reprocessing_transcript(tmp_path: Path):
    archive_root = tmp_path / "archive"
    day = archive_root / "2026" / "10" / "16"
    videos_dir = day / "videos"
    video_dir = videos_dir / "testvideo001"
    video_dir.mkdir(parents=True)
    (video_dir / "metadata.json").write_text(
        json.dumps(
            {
                "video_id": "testvideo001",
                "title": "Cached",
                "youtube_url": "https://www.youtube.com/watch?v=testvideo001",
                "published_at_utc": datetime.now(timezone.utc).isoformat(),
                "transcript_status": "success",
                "transcript_provider": "youtube_transcript_api",
            }
        ),
        encoding="utf-8",
    )
    (video_dir / "transcript.md").write_text(
        "# Cached\n\nURL: x\n\nBody text.",
        encoding="utf-8",
    )
    (video_dir / "summary.md").write_text("## Main concept\nSummary.", encoding="utf-8")
    (day / "manifest.json").write_text(
        json.dumps(
            {
                "digest_date": "2026-10-16",
                "email_status": "failed",
                "videos": [{"video_id": "testvideo001"}],
            }
        ),
        encoding="utf-8",
    )
    (archive_root / "index.json").write_text(
        json.dumps(
            {
                "videos": {
                    "testvideo001": {"transcript_status": "success"},
                }
            }
        ),
        encoding="utf-8",
    )

    config = PipelineConfig(
        youtube_api_key=None,
        youtube_channel_handle="aiDotEngineer",
        openai_api_key=None,
        openai_model="gpt-4o-mini",
        archive_root=archive_root,
        archive_github_repo=None,
        archive_github_token=None,
        email_smtp_host=None,
        email_smtp_port=587,
        email_smtp_user=None,
        email_smtp_password=None,
        email_from=None,
        email_to=None,
        discovery_lookback_hours=24,
        discovery_recovery_days=7,
        send_email_on_zero_videos=False,
    )
    now = datetime.now(timezone.utc)
    record = DiscoveryRecord(
        video_id="testvideo001",
        channel_id="UC",
        title="Cached",
        youtube_url="https://www.youtube.com/watch?v=testvideo001",
        published_at_utc=now,
        discovery_timestamp_utc=now,
        video_type=VideoType.STANDARD,
        status=DiscoveryStatus.DISCOVERED,
        discovery_category=DiscoveryCategory.MANUAL_VIDEO_ID,
    )

    with patch("pipeline.daily_run._discover", return_value=[record]):
        with patch("pipeline.daily_run.fetch_transcript") as fetch_mock:
            result = run_daily_digest(
                config,
                digest_date=datetime(2026, 10, 16, tzinfo=timezone.utc).date(),
                dry_run=True,
                mock_llm=True,
                force=False,
                video_id="testvideo001",
            )
            fetch_mock.assert_not_called()

    assert result.videos_processed == 1
    assert result.email_status == "preview"
    assert (day / "transcripts-combined.docx").exists()
