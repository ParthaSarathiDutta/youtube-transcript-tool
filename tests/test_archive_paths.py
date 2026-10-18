"""Archive folder naming, migration, and video-id dedupe."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.archive_paths import (
    build_video_folder_name,
    migrate_archive_video_folders,
    migrate_video_folder,
    resolve_video_dir,
    slugify_title_for_folder,
)
from pipeline.archive_store import (
    load_hydrated_video,
    save_index,
    video_already_processed,
    write_video_artifacts,
)


def test_slugify_handles_special_chars_and_length():
    name = build_video_folder_name(
        "The AI Bill Is Too Damn High: Tokenomics 101 — Eugene Cheah, Featherless AI",
        "Ti-BmGU1_mQ",
    )
    assert name.endswith("__Ti-BmGU1_mQ")
    assert ":" not in name
    assert len(name) <= 120


def test_migrate_renames_legacy_folder_without_duplicating(tmp_path: Path):
    videos_dir = tmp_path / "videos"
    legacy = videos_dir / "abc12345678"
    legacy.mkdir(parents=True)
    transcript = "Full transcript body."
    (legacy / "transcript.md").write_text(
        "# Title\n\nURL: x\n\n" + transcript,
        encoding="utf-8",
    )
    (legacy / "metadata.json").write_text(
        json.dumps(
            {
                "video_id": "abc12345678",
                "title": "Hello: World & Friends",
            }
        ),
        encoding="utf-8",
    )

    folder = migrate_video_folder(
        videos_dir, video_id="abc12345678", title="Hello: World & Friends"
    )
    assert folder.endswith("__abc12345678")
    assert not legacy.exists()
    assert (videos_dir / folder / "transcript.md").read_text(encoding="utf-8").endswith(
        transcript
    )
    assert resolve_video_dir(videos_dir, "abc12345678") == videos_dir / folder


def test_migrate_skips_when_target_already_exists(tmp_path: Path):
    videos_dir = tmp_path / "videos"
    title = "Duplicate Test"
    folder_name = build_video_folder_name(title, "dup12345678")
    target = videos_dir / folder_name
    target.mkdir(parents=True)
    (target / "transcript.md").write_text("# T\n\nURL: x\n\ncanonical", encoding="utf-8")
    legacy = videos_dir / "dup12345678"
    legacy.mkdir(parents=True)
    (legacy / "transcript.md").write_text("# T\n\nURL: x\n\ncanonical", encoding="utf-8")
    (legacy / "metadata.json").write_text(
        json.dumps({"video_id": "dup12345678", "title": title}),
        encoding="utf-8",
    )

    result = migrate_video_folder(videos_dir, video_id="dup12345678", title=title)
    assert result == folder_name
    assert not legacy.exists()
    assert len(list(videos_dir.iterdir())) == 1


def test_migrate_archive_updates_index_and_load_hydrated(tmp_path: Path):
    archive_root = tmp_path / "archive"
    videos_dir = archive_root / "2026" / "10" / "17" / "videos"
    legacy = videos_dir / "vid12345678"
    legacy.mkdir(parents=True)
    (legacy / "metadata.json").write_text(
        json.dumps(
            {
                "video_id": "vid12345678",
                "title": "Hydrated Title",
                "youtube_url": "https://www.youtube.com/watch?v=vid12345678",
                "transcript_status": "success",
                "transcript_provider": "youtube_transcript_api",
                "summary_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    (legacy / "transcript.md").write_text(
        "# Hydrated Title\n\nURL: x\n\nBody text.",
        encoding="utf-8",
    )
    index = {"videos": {"vid12345678": {"transcript_status": "success"}}}
    save_index(archive_root, index)

    migrated = migrate_archive_video_folders(archive_root, index)
    assert migrated == 1
    assert index["videos"]["vid12345678"]["archive_folder"].endswith("__vid12345678")

    hydrated = load_hydrated_video(
        videos_dir,
        "vid12345678",
        folder_hint=index["videos"]["vid12345678"]["archive_folder"],
    )
    assert hydrated is not None
    assert "Body text" in hydrated.doc_item.transcript.display_text


def test_write_artifacts_uses_readable_folder(tmp_path: Path):
    videos_dir = tmp_path / "videos"
    write_video_artifacts(
        videos_dir=videos_dir,
        video_id="newvideo0001",
        metadata={
            "video_id": "newvideo0001",
            "title": "Brand New Talk",
            "youtube_url": "https://youtu.be/newvideo0001",
        },
        transcript_md="# Brand New Talk\n\nURL: x\n\nText.",
    )
    folder = resolve_video_dir(videos_dir, "newvideo0001")
    assert folder is not None
    assert folder.name.startswith("Brand-New-Talk__")


def test_deduplication_still_uses_video_id_in_index(tmp_path: Path):
    index = {
        "videos": {
            "abc12345678": {
                "transcript_status": "success",
                "archive_folder": "Some-Title__abc12345678",
            }
        }
    }
    assert video_already_processed(index, "abc12345678") is True
    assert video_already_processed(index, "other000000") is False
