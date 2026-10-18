"""Evals playlist Phase 2 sync."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from discovery.playlist import PlaylistVideoRef
from pipeline.archive_store import video_archived_for_daily_digest
from pipeline.config import PipelineConfig
from pipeline.evals_playlist_sync import (
    EVALS_ARCHIVE_SUBPATH,
    PlaylistVideoSyncEntry,
    copy_daily_transcript_to_evals,
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


def test_resolve_needs_copy_when_daily_exists_but_not_evals(tmp_path: Path):
    archive_root = tmp_path
    evals_dir = archive_root / EVALS_ARCHIVE_SUBPATH
    video_id = "archived11111"
    folder = f"Talk__{video_id}"
    daily_dir = archive_root / "2026" / "10" / "17" / "videos" / folder
    daily_dir.mkdir(parents=True)
    (daily_dir / "transcript.md").write_text("daily transcript", encoding="utf-8")
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
    assert entry.action == "needs_copy"


def test_sync_copies_daily_transcript_into_evals_folder(tmp_path: Path):
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    video_id = "archived11111"
    folder = f"Talk__{video_id}"
    daily_dir = archive_root / "2026" / "10" / "17" / "videos" / folder
    daily_dir.mkdir(parents=True)
    (daily_dir / "transcript.md").write_text("daily body", encoding="utf-8")
    (daily_dir / "metadata.json").write_text(
        json.dumps(
            {
                "video_id": video_id,
                "transcript_provider": "transcriptapi",
                "published_at_utc": "2024-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (archive_root / "index.json").write_text(
        json.dumps(
            {
                "videos": {
                    video_id: {
                        "transcript_status": "success",
                        "archive_folder": folder,
                        "transcript_generated_at_utc": "2025-06-01T12:00:00+00:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    refs = [PlaylistVideoRef(video_id, "Talk", 1)]
    client = MagicMock()
    client.get_videos_details.return_value = {}

    def fail_fetch(_vid: str) -> TranscriptResult:
        raise AssertionError("must not download when daily copy exists")

    with patch("pipeline.evals_playlist_sync.YouTubeDataApiClient", return_value=client):
        with patch("pipeline.evals_playlist_sync.list_playlist_videos", return_value=("Evals", refs)):
            result = sync_evals_playlist(
                _config(archive_root),
                dry_run=False,
                pull=False,
                fetch_transcript_fn=fail_fetch,
            )

    assert result.copied == 1
    evals_dir = archive_root / EVALS_ARCHIVE_SUBPATH
    evals_transcript = evals_dir / folder / "transcript.md"
    assert evals_transcript.is_file()
    assert evals_transcript.read_text(encoding="utf-8") == "daily body"
    meta = json.loads((evals_dir / folder / "metadata.json").read_text(encoding="utf-8"))
    assert meta["transcript_generated_at_utc"] == "2025-06-01T12:00:00+00:00"
    assert meta["transcript_provider"] == "transcriptapi"
    payload = json.loads((evals_dir / "playlist-index.json").read_text(encoding="utf-8"))
    assert payload["videos"][0]["transcript_location"].startswith("responsiv/evals/")


def test_copy_uses_null_generation_time_when_unknown(tmp_path: Path):
    archive_root = tmp_path / "archive"
    evals_dir = archive_root / EVALS_ARCHIVE_SUBPATH
    video_id = "archived11111"
    folder = f"Talk__{video_id}"
    daily_dir = archive_root / "2026" / "10" / "17" / "videos" / folder
    daily_dir.mkdir(parents=True)
    (daily_dir / "transcript.md").write_text("body", encoding="utf-8")
    ref = PlaylistVideoRef(video_id, "Talk", 1)
    entry = copy_daily_transcript_to_evals(
        archive_root=archive_root,
        evals_dir=evals_dir,
        ref=ref,
        detail=None,
        daily_dir=daily_dir,
        index={"videos": {}},
    )
    meta = json.loads((evals_dir / folder / "metadata.json").read_text(encoding="utf-8"))
    assert meta["transcript_generated_at_utc"] is None
    assert entry.action == "copied"


def test_repeated_sync_skips_without_download_or_copy(tmp_path: Path):
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
        display_text="Hello transcript body long enough",
        provider="youtube-api",
    )
    fetch_calls: list[str] = []

    def counting_fetch(vid: str) -> TranscriptResult:
        fetch_calls.append(vid)
        return transcript

    with patch("pipeline.evals_playlist_sync.YouTubeDataApiClient", return_value=client):
        with patch("pipeline.evals_playlist_sync.list_playlist_videos", return_value=("Evals", refs)):
            first = sync_evals_playlist(
                _config(archive_root),
                dry_run=False,
                pull=False,
                fetch_transcript_fn=counting_fetch,
            )
            second = sync_evals_playlist(
                _config(archive_root),
                dry_run=False,
                pull=False,
                fetch_transcript_fn=counting_fetch,
            )

    assert first.extracted == 1
    assert second.skipped == 1
    assert fetch_calls == ["missing22222"]


def test_sync_does_not_overwrite_existing_evals_transcript(tmp_path: Path):
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    video_id = "archived11111"
    folder = f"Talk__{video_id}"
    evals_dir = archive_root / EVALS_ARCHIVE_SUBPATH / folder
    evals_dir.mkdir(parents=True)
    (evals_dir / "transcript.md").write_text("evals original", encoding="utf-8")
    daily_dir = archive_root / "2026" / "10" / "17" / "videos" / folder
    daily_dir.mkdir(parents=True)
    (daily_dir / "transcript.md").write_text("daily newer", encoding="utf-8")
    (archive_root / "index.json").write_text(json.dumps({"videos": {}}), encoding="utf-8")
    refs = [PlaylistVideoRef(video_id, "Talk", 1)]
    client = MagicMock()
    client.get_videos_details.return_value = {}

    with patch("pipeline.evals_playlist_sync.YouTubeDataApiClient", return_value=client):
        with patch("pipeline.evals_playlist_sync.list_playlist_videos", return_value=("Evals", refs)):
            result = sync_evals_playlist(
                _config(archive_root),
                dry_run=False,
                pull=False,
                fetch_transcript_fn=lambda _v: pytest.fail("no fetch"),
            )

    assert result.skipped == 1
    assert (evals_dir / "transcript.md").read_text(encoding="utf-8") == "evals original"


def test_sync_extracts_new_video_and_points_index_to_evals(tmp_path: Path):
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    (archive_root / "index.json").write_text(json.dumps({"videos": {}}), encoding="utf-8")
    refs = [PlaylistVideoRef("missing22222", "Need transcript", 1, "2024-06-01T00:00:00+00:00")]
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
                fetch_transcript_fn=lambda _vid: transcript,
            )

    assert result.extracted == 1
    evals_dir = archive_root / EVALS_ARCHIVE_SUBPATH
    loc = result.videos[0].transcript_location
    assert loc is not None and loc.startswith("responsiv/evals/")
    assert (evals_dir / "playlist-index.json").is_file()
    index_md = (evals_dir / "index.md").read_text(encoding="utf-8")
    assert "responsiv/evals/" in index_md
    assert "../../2026/" not in index_md


def test_sync_dry_run_does_not_write_transcripts(tmp_path: Path):
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    (archive_root / "index.json").write_text(json.dumps({"videos": {}}), encoding="utf-8")
    refs = [PlaylistVideoRef("missing22222", "Need transcript", 1)]

    def fake_fetch(_video_id: str) -> TranscriptResult:
        raise AssertionError("fetch_transcript should not run in dry-run")

    client = MagicMock()
    client.get_videos_details.return_value = {}
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


def test_markdown_transcript_link_uses_paths_relative_to_index_md(tmp_path: Path):
    archive_root = tmp_path
    evals_dir = archive_root / EVALS_ARCHIVE_SUBPATH
    evals_dir.mkdir(parents=True)
    local = evals_dir / "Talk__vid12345678" / "transcript.md"
    local.parent.mkdir(parents=True)
    local.write_text("t", encoding="utf-8")
    local_link = markdown_transcript_link(
        evals_dir=evals_dir,
        archive_root=archive_root,
        transcript_location="responsiv/evals/Talk__vid12345678/transcript.md",
    )
    assert local_link.endswith("(Talk__vid12345678/transcript.md)")


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
