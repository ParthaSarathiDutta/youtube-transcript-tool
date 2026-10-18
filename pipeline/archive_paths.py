"""Readable archive folder names: slugified title + video id."""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
from pathlib import Path

FOLDER_ID_SEPARATOR = "__"
MAX_TITLE_SLUG_LEN = 80
MAX_FOLDER_NAME_LEN = 120


def slugify_title_for_folder(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title.strip())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^\w\s-]", "", ascii_text)
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not cleaned:
        return "video"
    return cleaned[:MAX_TITLE_SLUG_LEN].strip("-") or "video"


def build_video_folder_name(title: str, video_id: str) -> str:
    slug = slugify_title_for_folder(title)
    name = f"{slug}{FOLDER_ID_SEPARATOR}{video_id}"
    if len(name) <= MAX_FOLDER_NAME_LEN:
        return name
    trim = MAX_FOLDER_NAME_LEN - len(FOLDER_ID_SEPARATOR) - len(video_id)
    if trim < 1:
        return video_id
    slug = slug[:trim].rstrip("-") or "video"
    return f"{slug}{FOLDER_ID_SEPARATOR}{video_id}"


def parse_video_id_from_folder(folder_name: str) -> str | None:
    if FOLDER_ID_SEPARATOR in folder_name:
        video_id = folder_name.rsplit(FOLDER_ID_SEPARATOR, 1)[-1]
        return video_id or None
    return folder_name or None


def resolve_video_dir(
    videos_dir: Path,
    video_id: str,
    *,
    folder_hint: str | None = None,
) -> Path | None:
    if not videos_dir.is_dir():
        return None
    if folder_hint:
        hinted = videos_dir / folder_hint
        if hinted.is_dir() and _folder_matches_video_id(hinted.name, video_id):
            return hinted
    legacy = videos_dir / video_id
    if legacy.is_dir():
        return legacy
    suffix = f"{FOLDER_ID_SEPARATOR}{video_id}"
    matches = [
        child
        for child in videos_dir.iterdir()
        if child.is_dir() and _folder_matches_video_id(child.name, video_id)
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    for child in matches:
        if child.name.endswith(suffix):
            return child
    return matches[0]


def _folder_matches_video_id(folder_name: str, video_id: str) -> bool:
    parsed = parse_video_id_from_folder(folder_name)
    return parsed == video_id


def migrate_video_folder(
    videos_dir: Path,
    *,
    video_id: str,
    title: str,
) -> str:
    """Rename legacy id-only folder to title+id; skip if already migrated."""
    target_name = build_video_folder_name(title, video_id)
    target_dir = videos_dir / target_name
    source_dir = resolve_video_dir(videos_dir, video_id)
    if source_dir is None:
        return target_name
    if source_dir.name == target_name:
        return target_name
    if target_dir.exists():
        if _same_video_dir(source_dir, target_dir):
            if source_dir != target_dir:
                shutil.rmtree(source_dir)
        return target_name
    source_dir.rename(target_dir)
    return target_name


def _same_video_dir(left: Path, right: Path) -> bool:
    left_transcript = left / "transcript.md"
    right_transcript = right / "transcript.md"
    if not left_transcript.exists() or not right_transcript.exists():
        return False
    return left_transcript.read_bytes() == right_transcript.read_bytes()


def migrate_archive_video_folders(archive_root: Path, index: dict) -> int:
    """Rename video folders under archive_root; update index archive_folder hints."""
    migrated = 0
    videos_index = index.setdefault("videos", {})
    for videos_dir in archive_root.glob("*/*/*/videos"):
        if not videos_dir.is_dir():
            continue
        for child in list(videos_dir.iterdir()):
            if not child.is_dir():
                continue
            video_id, title = _read_folder_identity(child)
            if not video_id:
                continue
            before = child.name
            folder_name = migrate_video_folder(
                videos_dir, video_id=video_id, title=title or video_id
            )
            if before != folder_name:
                migrated += 1
            entry = videos_index.setdefault(video_id, {})
            entry["archive_folder"] = folder_name
            if title:
                entry.setdefault("title", title)
    return migrated


def _read_folder_identity(folder: Path) -> tuple[str | None, str | None]:
    metadata_path = folder / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata = {}
        video_id = metadata.get("video_id") or parse_video_id_from_folder(folder.name)
        title = metadata.get("title")
        return (str(video_id) if video_id else None, str(title) if title else None)
    video_id = parse_video_id_from_folder(folder.name)
    return (video_id, None)
