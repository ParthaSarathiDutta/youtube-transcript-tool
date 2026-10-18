"""Archive folder naming, migration, and video-id dedupe."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.archive_paths import (
    build_video_folder_name,
    consolidate_video_folders,
    list_video_dirs,
    migrate_archive_video_folders,
    migrate_video_folder,
    resolve_video_dir,
    update_day_manifest_video_paths,
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
    assert len(list_video_dirs(videos_dir, "abc12345678")) == 1
    assert (videos_dir / folder / "transcript.md").read_text(encoding="utf-8").endswith(
        transcript
    )
    assert resolve_video_dir(videos_dir, "abc12345678") == videos_dir / folder


def test_collision_merges_unique_summary_before_removing_legacy(tmp_path: Path):
    videos_dir = tmp_path / "videos"
    title = "Duplicate Test"
    folder_name = build_video_folder_name(title, "dup12345678")
    target = videos_dir / folder_name
    target.mkdir(parents=True)
    (target / "transcript.md").write_text("# T\n\nURL: x\n\ncanonical", encoding="utf-8")
    legacy = videos_dir / "dup12345678"
    legacy.mkdir(parents=True)
    (legacy / "transcript.md").write_text("# T\n\nURL: x\n\ncanonical", encoding="utf-8")
    (legacy / "summary.md").write_text("## Main concept\nOnly in legacy.", encoding="utf-8")
    (legacy / "metadata.json").write_text(
        json.dumps({"video_id": "dup12345678", "title": title, "extra": "legacy"}),
        encoding="utf-8",
    )

    result = consolidate_video_folders(videos_dir, video_id="dup12345678", title=title)
    assert result == folder_name
    assert len(list_video_dirs(videos_dir, "dup12345678")) == 1
    assert not legacy.exists()
    canonical = videos_dir / folder_name
    assert (canonical / "summary.md").read_text(encoding="utf-8").startswith("## Main concept")
    meta = json.loads((canonical / "metadata.json").read_text(encoding="utf-8"))
    assert meta.get("extra") == "legacy"


def test_collision_preserves_differing_transcript_in_backup(tmp_path: Path):
    videos_dir = tmp_path / "videos"
    title = "Conflict Talk"
    folder_name = build_video_folder_name(title, "conf1234567")
    target = videos_dir / folder_name
    target.mkdir(parents=True)
    (target / "transcript.md").write_text("# T\n\nURL: x\n\nversion A", encoding="utf-8")
    legacy = videos_dir / "conf1234567"
    legacy.mkdir(parents=True)
    (legacy / "transcript.md").write_text("# T\n\nURL: x\n\nversion B", encoding="utf-8")

    consolidate_video_folders(videos_dir, video_id="conf1234567", title=title)
    canonical = videos_dir / folder_name
    assert (canonical / "transcript.md").read_text(encoding="utf-8").endswith("version A")
    backups = list(canonical.glob("transcript.from-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8").endswith("version B")


def test_manifest_paths_updated_after_folder_consolidation(tmp_path: Path):
    day_dir = tmp_path / "2026" / "10" / "17"
    videos_dir = day_dir / "videos"
    legacy = videos_dir / "man12345678"
    legacy.mkdir(parents=True)
    (legacy / "transcript.md").write_text("# T\n\nURL: x\n\nBody.", encoding="utf-8")
    (legacy / "metadata.json").write_text(
        json.dumps({"video_id": "man12345678", "title": "Manifest Video"}),
        encoding="utf-8",
    )
    old_transcript_path = str(legacy / "transcript.md")
    (day_dir / "manifest.json").write_text(
        json.dumps(
            {
                "digest_date": "2026-10-17",
                "videos": [
                    {
                        "video_id": "man12345678",
                        "transcript_path": old_transcript_path,
                        "metadata_path": str(legacy / "metadata.json"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    folder_name = migrate_video_folder(
        videos_dir, video_id="man12345678", title="Manifest Video"
    )
    assert update_day_manifest_video_paths(day_dir, "man12345678", folder_name)
    manifest = json.loads((day_dir / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["videos"][0]
    assert entry["archive_folder"] == folder_name
    assert entry["transcript_path"].endswith(f"{folder_name}/transcript.md")
    assert Path(entry["transcript_path"]).is_file()


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
    assert len(list_video_dirs(videos_dir, "vid12345678")) == 1
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
    assert len(list_video_dirs(videos_dir, "newvideo0001")) == 1


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
