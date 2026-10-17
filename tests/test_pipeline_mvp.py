"""MVP pipeline tests (mocked externals)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from discovery.models import (
    DiscoveryCategory,
    DiscoveryRecord,
    DiscoveryStatus,
    VideoType,
)
from document_writer import DailyVideoDocument, write_daily_combined_document
from pipeline.archive_store import load_index, save_index, video_already_processed
from pipeline.config import PipelineConfig
from pipeline.daily_run import run_daily_digest
from pipeline.email_delivery import send_digest_email
from pipeline.summarizer import summarize_transcript
from transcript_service import TranscriptResult


def _record(video_id: str = "testvideo001") -> DiscoveryRecord:
    now = datetime.now(timezone.utc)
    return DiscoveryRecord(
        video_id=video_id,
        channel_id="UCx",
        title="Test Video",
        youtube_url=f"https://www.youtube.com/watch?v={video_id}",
        published_at_utc=now,
        discovery_timestamp_utc=now,
        video_type=VideoType.STANDARD,
        status=DiscoveryStatus.DISCOVERED,
        discovery_category=DiscoveryCategory.MANUAL_VIDEO_ID,
    )


def test_mocked_openai_summarization():
    result = summarize_transcript(
        title="T",
        youtube_url="https://youtu.be/x",
        transcript_text="Speaker discusses retrieval augmented generation.",
        api_key=None,
        model="gpt-4o-mini",
        mock=True,
    )
    assert result.mocked is True
    assert "Mock summary" in result.markdown


def test_combined_word_document_generation(tmp_path: Path):
    transcript = TranscriptResult(
        segments=[],
        language_code="en",
        is_generated=True,
        display_text="Hello transcript body.",
        available=True,
        provider="test",
    )
    item = DailyVideoDocument(
        video_id="abc12345678",
        title="Sample",
        url="https://www.youtube.com/watch?v=abc12345678",
        published_at_utc=datetime.now(timezone.utc),
        transcript=transcript,
    )
    out = write_daily_combined_document("Digest", [item], tmp_path / "combined.docx")
    assert out.exists()
    assert out.stat().st_size > 1000


def test_mocked_email_preview(tmp_path: Path):
    result = send_digest_email(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="u",
        smtp_password="p",
        email_from="from@example.com",
        email_to="to@example.com",
        subject="Test",
        plain_body="Body",
        html_body="<p>Body</p>",
        attachment_path=None,
        dry_run=True,
        preview_dir=tmp_path,
    )
    assert result.status == "preview"
    assert (tmp_path / "email-preview.txt").exists()


def test_duplicate_processing_skips_indexed_video(tmp_path: Path):
    index = {"videos": {"dup12345678": {"transcript_status": "success"}}}
    save_index(tmp_path, index)
    loaded = load_index(tmp_path)
    assert video_already_processed(loaded, "dup12345678") is True


def test_pipeline_vertical_slice_with_mocks(tmp_path: Path):
    config = PipelineConfig(
        youtube_api_key=None,
        youtube_channel_handle="aiDotEngineer",
        openai_api_key=None,
        openai_model="gpt-4o-mini",
        archive_root=tmp_path / "archive",
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
    transcript = TranscriptResult(
        segments=[],
        language_code="en",
        is_generated=False,
        display_text="Grounded transcript text about agents and evals.",
        available=True,
        provider="youtube_transcript_api",
    )

    with patch("pipeline.daily_run._discover", return_value=[_record()]):
        with patch("pipeline.daily_run.fetch_transcript", return_value=transcript):
            result = run_daily_digest(
                config,
                dry_run=True,
                mock_llm=True,
                force=True,
                video_id="testvideo001",
            )

    assert result.videos_processed == 1
    day_dir = Path(result.archive_dir)
    assert (day_dir / "manifest.json").exists()
    assert (day_dir / "transcripts-combined.docx").exists()
    assert (day_dir / "daily-summary.md").exists()
    video_dir = day_dir / "videos" / "testvideo001"
    assert (video_dir / "transcript.md").exists()
    assert (video_dir / "summary.md").exists()
    manifest = json.loads((day_dir / "manifest.json").read_text())
    assert manifest["email_status"] == "preview"
