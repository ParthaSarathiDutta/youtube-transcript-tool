"""Evals playlist Phase 2 sync."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from discovery.playlist import PlaylistVideoRef
from pipeline.config import PipelineConfig
from pipeline.archive_store import video_archived_for_daily_digest
from pipeline.evals_playlist_sync import (
    EVALS_ARCHIVE_SUBPATH,
    PlaylistVideoSyncEntry,
    locate_global_transcript_dir,
    markdown_transcript_link,
    resolve_playlist_video_entry,
    sync_evals_playlist,
    write_playlist_index_artifacts,
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


def test_locate_global_transcript_dir_ignores_git_sync_mirror(tmp_path: Path):
    video_id = "archived11111"
    folder = f"Talk__{video_id}"
    mirror = tmp_path / ".git-sync" / "2026" / "10" / "17" / "videos" / folder
    mirror.mkdir(parents=True)
    (mirror / "transcript.md").write_text("mirror", encoding="utf-8")
    canonical = tmp_path / "2026" / "10" / "17" / "videos" / folder
    canonical.mkdir(parents=True)
    (canonical / "transcript.md").write_text("canonical", encoding="utf-8")
    index = {"videos": {video_id: {"transcript_status": "success", "archive_folder": folder}}}
    found = locate_global_transcript_dir(tmp_path, video_id, index)
    assert found == canonical


def test_locate_global_transcript_dir_finds_daily_archive(tmp_path: Path):
    video_id = "archived11111"
    folder = f"Talk-Title__{video_id}"
    videos_dir = tmp_path / "2026" / "10" / "17" / "videos"
    video_dir = videos_dir / folder
    video_dir.mkdir(parents=True)
    (video_dir / "transcript.md").write_text("# t\n\nhello", encoding="utf-8")
    index = {
        "videos": {
            video_id: {
                "transcript_status": "success",
                "archive_folder": folder,
            }
        }
    }
    found = locate_global_transcript_dir(tmp_path, video_id, index)
    assert found == video_dir


def test_resolve_references_global_transcript_without_copy(tmp_path: Path):
    archive_root = tmp_path
    evals_dir = archive_root / EVALS_ARCHIVE_SUBPATH
    video_id = "archived11111"
    folder = f"Talk__{video_id}"
    videos_dir = archive_root / "2026" / "10" / "17" / "videos" / folder
    videos_dir.mkdir(parents=True)
    (videos_dir / "transcript.md").write_text("text", encoding="utf-8")
    (videos_dir / "metadata.json").write_text(
        json.dumps(
            {
                "video_id": video_id,
                "transcript_generated_at_utc": "2025-01-01T00:00:00+00:00",
                "transcript_provider": "youtube-api",
                "youtube_published_at_utc": "2024-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    index = {"videos": {video_id: {"transcript_status": "success", "archive_folder": folder}}}
    ref = PlaylistVideoRef(video_id=video_id, title="Talk", position=1)
    entry = resolve_playlist_video_entry(
        archive_root=archive_root,
        evals_dir=evals_dir,
        index=index,
        ref=ref,
        detail=None,
        prior={},
    )
    assert entry.action == "referenced"
    assert entry.transcript_source == "global_archive"
    assert not (evals_dir / folder).exists()


def test_sync_dry_run_does_not_write_transcripts(tmp_path: Path):
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    (archive_root / "index.json").write_text(json.dumps({"videos": {}}), encoding="utf-8")
    refs = [
        PlaylistVideoRef("missing22222", "Need transcript", 1),
    ]
    client = MagicMock()
    client.get_videos_details.return_value = {}

    def fake_fetch(_video_id: str) -> TranscriptResult:
        raise AssertionError("fetch_transcript should not run in dry-run")

    with patch("pipeline.evals_playlist_sync.YouTubeDataApiClient", return_value=client):
        with patch("pipeline.evals_playlist_sync.list_playlist_videos", return_value=("Evals", refs)):
            result = sync_evals_playlist(
                _config(archive_root),
                dry_run=True,
                pull=False,
                fetch_transcript_fn=fake_fetch,
            )
    assert result.videos[0].action == "would_extract"
    assert not (archive_root / EVALS_ARCHIVE_SUBPATH).exists()


def test_sync_extracts_one_video_and_writes_metadata(tmp_path: Path):
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    (archive_root / "index.json").write_text(json.dumps({"videos": {}}), encoding="utf-8")
    refs = [
        PlaylistVideoRef("missing22222", "Need transcript", 1, "2024-06-01T00:00:00+00:00"),
    ]
    client = MagicMock()
    client.get_videos_details.return_value = {}

    transcript = TranscriptResult(
        segments=[],
        language_code="en",
        is_generated=True,
        display_text="Hello transcript body long enough",
        provider="youtube-api",
    )

    with patch("pipeline.evals_playlist_sync.YouTubeDataApiClient", return_value=client):
        with patch("pipeline.evals_playlist_sync.list_playlist_videos", return_value=("Evals", refs)):
            result = sync_evals_playlist(
                _config(archive_root),
                dry_run=False,
                pull=False,
                push=False,
                video_id="missing22222",
                fetch_transcript_fn=lambda _vid: transcript,
            )

    assert result.extracted == 1
    evals_dir = archive_root / EVALS_ARCHIVE_SUBPATH
    meta_files = list(evals_dir.glob("*/metadata.json"))
    assert len(meta_files) == 1
    metadata = json.loads(meta_files[0].read_text(encoding="utf-8"))
    assert metadata["video_id"] == "missing22222"
    assert metadata["transcript_provider"] == "youtube-api"
    assert metadata["youtube_published_at_utc"]
    assert metadata["transcript_generated_at_utc"]
    assert (evals_dir / "playlist-index.json").is_file()
    assert (evals_dir / "index.md").is_file()


def test_markdown_transcript_link_uses_paths_relative_to_index_md(tmp_path: Path):
    archive_root = tmp_path
    evals_dir = archive_root / EVALS_ARCHIVE_SUBPATH
    evals_dir.mkdir(parents=True)
    local = evals_dir / "Talk__vid12345678" / "transcript.md"
    local.parent.mkdir(parents=True)
    local.write_text("t", encoding="utf-8")
    daily = archive_root / "2026" / "10" / "17" / "videos" / "Talk__vid12345678" / "transcript.md"
    daily.parent.mkdir(parents=True)
    daily.write_text("t", encoding="utf-8")
    local_link = markdown_transcript_link(
        evals_dir=evals_dir,
        archive_root=archive_root,
        transcript_location="responsiv/evals/Talk__vid12345678/transcript.md",
    )
    assert local_link.endswith("(Talk__vid12345678/transcript.md)")
    daily_link = markdown_transcript_link(
        evals_dir=evals_dir,
        archive_root=archive_root,
        transcript_location="2026/10/17/videos/Talk__vid12345678/transcript.md",
    )
    assert "../../2026/10/17/videos/Talk__vid12345678/transcript.md" in daily_link


def test_playlist_only_success_does_not_block_daily_when_no_daily_folder(
    tmp_path: Path,
):
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    video_id = "playlist001"
    evals_dir = archive_root / EVALS_ARCHIVE_SUBPATH / f"Talk__{video_id}"
    evals_dir.mkdir(parents=True)
    (evals_dir / "transcript.md").write_text("playlist only", encoding="utf-8")
    index = {
        "videos": {
            video_id: {
                "transcript_status": "success",
                "evals_playlist_transcript_status": "success",
            }
        }
    }
    assert video_archived_for_daily_digest(
        index, video_id, archive_root=archive_root
    ) is False


def test_sync_keeps_missing_youtube_published_at_null(tmp_path: Path):
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    (archive_root / "index.json").write_text(json.dumps({"videos": {}}), encoding="utf-8")
    refs = [PlaylistVideoRef("missing22222", "Need transcript", 1, None)]
    client = MagicMock()
    client.get_videos_details.return_value = {}
    transcript = TranscriptResult(
        segments=[],
        language_code="en",
        is_generated=True,
        display_text="Hello transcript body long enough",
        provider="youtube-api",
    )
    with patch("pipeline.evals_playlist_sync.YouTubeDataApiClient", return_value=client):
        with patch("pipeline.evals_playlist_sync.list_playlist_videos", return_value=("Evals", refs)):
            sync_evals_playlist(
                _config(archive_root),
                dry_run=False,
                pull=False,
                push=False,
                video_id="missing22222",
                fetch_transcript_fn=lambda _vid: transcript,
            )
    meta = json.loads(
        next((archive_root / EVALS_ARCHIVE_SUBPATH).glob("*/metadata.json")).read_text(
            encoding="utf-8"
        )
    )
    assert meta.get("youtube_published_at_utc") is None


def test_metadata_merge_preserves_transcript_generated_at(tmp_path: Path):
    archive_root = tmp_path
    evals_dir = archive_root / EVALS_ARCHIVE_SUBPATH
    folder = evals_dir / "Talk__vid12345678"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(
        json.dumps({"transcript_generated_at_utc": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    entries = [
        PlaylistVideoSyncEntry(
            video_id="vid12345678",
            title="Talk",
            youtube_url="https://www.youtube.com/watch?v=vid12345678",
            position=1,
            youtube_published_at_utc="2024-01-01T00:00:00+00:00",
            transcript_generated_at_utc="2020-01-01T00:00:00+00:00",
            transcript_provider="youtube-api",
            transcript_location="responsiv/evals/Talk__vid12345678/transcript.md",
            transcript_source="evals_archive",
        )
    ]
    write_playlist_index_artifacts(
        archive_root=archive_root,
        playlist_id="PLtest",
        playlist_title="Evals",
        entries=entries,
        dry_run=False,
    )
    payload = json.loads((evals_dir / "playlist-index.json").read_text(encoding="utf-8"))
    assert payload["videos"][0]["transcript_generated_at_utc"] == "2020-01-01T00:00:00+00:00"
