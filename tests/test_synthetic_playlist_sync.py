"""Synthetic Data playlist sync (shared engine, separate destination)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from discovery.playlist import PlaylistVideoRef
from pipeline.config import PipelineConfig
from pipeline.playlist_catalog import (
    EVALS_PLAYLIST_TARGET,
    SYNTHETIC_DATA_PLAYLIST_TARGET,
)
from pipeline.playlist_sync import (
    locate_transcript_source_dir,
    resolve_playlist_video_entry,
    sync_playlist,
)
from transcript_service import TranscriptResult


def _config(archive_root: Path) -> PipelineConfig:
    return PipelineConfig(
        youtube_api_key="test-key",
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
        enable_summaries=False,
    )


def test_synthetic_copies_from_evals_folder_without_download(tmp_path: Path):
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    video_id = "archived11111"
    folder = f"Talk__{video_id}"
    evals_dir = archive_root / EVALS_PLAYLIST_TARGET.archive_subpath / folder
    evals_dir.mkdir(parents=True)
    (evals_dir / "transcript.md").write_text("from evals", encoding="utf-8")
    (evals_dir / "metadata.json").write_text(
        json.dumps({"transcript_provider": "test", "transcript_generated_at_utc": None}),
        encoding="utf-8",
    )
    (archive_root / "index.json").write_text(json.dumps({"videos": {}}), encoding="utf-8")
    refs = [PlaylistVideoRef(video_id, "Talk", 1)]
    client = MagicMock()
    client.get_videos_details.return_value = {}

    with patch("pipeline.playlist_sync.YouTubeDataApiClient", return_value=client):
        with patch("pipeline.playlist_sync.list_playlist_videos", return_value=("Synthetic", refs)):
            result = sync_playlist(
                _config(archive_root),
                SYNTHETIC_DATA_PLAYLIST_TARGET,
                dry_run=False,
                pull=False,
                fetch_transcript_fn=lambda _v: (_ for _ in ()).throw(
                    AssertionError("no download")
                ),
            )

    assert result.copied == 1
    synthetic_dir = archive_root / SYNTHETIC_DATA_PLAYLIST_TARGET.archive_subpath
    assert (synthetic_dir / folder / "transcript.md").read_text(encoding="utf-8") == "from evals"
    loc = result.videos[0].transcript_location
    assert loc is not None and loc.startswith("responsiv/synthetic data/")


def test_synthetic_resolve_needs_copy_from_daily_archive(tmp_path: Path):
    archive_root = tmp_path / "archive"
    synthetic_dir = archive_root / SYNTHETIC_DATA_PLAYLIST_TARGET.archive_subpath
    video_id = "archived11111"
    folder = f"Talk__{video_id}"
    daily_dir = archive_root / "2026" / "10" / "17" / "videos" / folder
    daily_dir.mkdir(parents=True)
    (daily_dir / "transcript.md").write_text("daily", encoding="utf-8")
    ref = PlaylistVideoRef(video_id, "Talk", 1)
    entry = resolve_playlist_video_entry(
        target=SYNTHETIC_DATA_PLAYLIST_TARGET,
        archive_root=archive_root,
        dest_dir=synthetic_dir,
        index={"videos": {}},
        ref=ref,
        detail=None,
        prior={},
    )
    assert entry.action == "needs_copy"


def test_locate_transcript_source_finds_evals_when_not_in_dest(tmp_path: Path):
    archive_root = tmp_path / "archive"
    video_id = "archived11111"
    folder = f"Talk__{video_id}"
    evals_video = archive_root / EVALS_PLAYLIST_TARGET.archive_subpath / folder
    evals_video.mkdir(parents=True)
    (evals_video / "transcript.md").write_text("x", encoding="utf-8")
    synthetic_dest = archive_root / SYNTHETIC_DATA_PLAYLIST_TARGET.archive_subpath
    found = locate_transcript_source_dir(
        archive_root,
        video_id,
        {"videos": {}},
        exclude_dest=synthetic_dest,
    )
    assert found == evals_video


def test_synthetic_repeated_sync_is_idempotent(tmp_path: Path):
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    (archive_root / "index.json").write_text(json.dumps({"videos": {}}), encoding="utf-8")
    refs = [PlaylistVideoRef("missing22222", "Need transcript", 1)]
    client = MagicMock()
    client.get_videos_details.return_value = {}
    transcript = TranscriptResult(
        segments=[],
        language_code="en",
        is_generated=True,
        display_text="Synthetic playlist transcript body text",
        provider="youtube-api",
    )
    calls: list[str] = []

    def fetch(vid: str) -> TranscriptResult:
        calls.append(vid)
        return transcript

    with patch("pipeline.playlist_sync.YouTubeDataApiClient", return_value=client):
        with patch("pipeline.playlist_sync.list_playlist_videos", return_value=("Synthetic", refs)):
            first = sync_playlist(
                _config(archive_root),
                SYNTHETIC_DATA_PLAYLIST_TARGET,
                dry_run=False,
                pull=False,
                fetch_transcript_fn=fetch,
            )
            second = sync_playlist(
                _config(archive_root),
                SYNTHETIC_DATA_PLAYLIST_TARGET,
                dry_run=False,
                pull=False,
                fetch_transcript_fn=fetch,
            )

    assert first.extracted == 1
    assert second.skipped == 1
    assert calls == ["missing22222"]
