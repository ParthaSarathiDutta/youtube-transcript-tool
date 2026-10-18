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
PROTECTED_ARTIFACTS = frozenset({"transcript.md", "summary.md", "metadata.json"})


def slugify_title_for_folder(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title.strip())
    ascii_text = normalized.encode("ascii", "ignore").decode("utf-8")
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


def list_video_dirs(videos_dir: Path, video_id: str) -> list[Path]:
    if not videos_dir.is_dir():
        return []
    return sorted(
        (
            child
            for child in videos_dir.iterdir()
            if child.is_dir() and _folder_matches_video_id(child.name, video_id)
        ),
        key=lambda path: path.name,
    )


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
    matches = list_video_dirs(videos_dir, video_id)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    suffix = f"{FOLDER_ID_SEPARATOR}{video_id}"
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
    """Consolidate all folders for video_id into one readable folder name."""
    return consolidate_video_folders(
        videos_dir, video_id=video_id, title=title
    )


def consolidate_video_folders(
    videos_dir: Path,
    *,
    video_id: str,
    title: str,
) -> str:
    if not videos_dir.is_dir():
        videos_dir.mkdir(parents=True, exist_ok=True)
    target_name = build_video_folder_name(title, video_id)
    canonical = videos_dir / target_name
    canonical.mkdir(parents=True, exist_ok=True)

    for folder in list_video_dirs(videos_dir, video_id):
        if folder.resolve() == canonical.resolve():
            continue
        merge_video_dir_into(canonical, folder)

    remaining = list_video_dirs(videos_dir, video_id)
    if len(remaining) != 1:
        raise RuntimeError(
            f"Expected one folder for {video_id}, found {len(remaining)}: "
            f"{[path.name for path in remaining]}"
        )
    return remaining[0].name


def merge_video_dir_into(dest: Path, src: Path) -> None:
    """Merge src into dest; preserve protected artifacts before removing src."""
    if not src.is_dir():
        return
    if src.resolve() == dest.resolve():
        return
    dest.mkdir(parents=True, exist_ok=True)

    for item in sorted(src.iterdir(), key=lambda path: path.name):
        if item.is_dir():
            nested_dest = dest / item.name
            merge_video_dir_into(nested_dest, item)
            continue
        dest_file = dest / item.name
        if not dest_file.exists():
            shutil.copy2(item, dest_file)
            continue
        if dest_file.read_bytes() == item.read_bytes():
            continue
        if item.name in PROTECTED_ARTIFACTS:
            if item.name == "metadata.json":
                _merge_metadata_files(dest_file, item, src_folder_name=src.name)
            else:
                _preserve_protected_conflict(dest, dest_file, src, item)
        else:
            alt = dest / f"{item.stem}.from-{src.name}{item.suffix}"
            if not alt.exists():
                shutil.copy2(item, alt)

    _assert_protected_contents_preserved(dest, src)
    shutil.rmtree(src)


def _load_json_dict(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _merge_metadata_files(
    dest_file: Path,
    src_file: Path,
    *,
    src_folder_name: str,
) -> None:
    dest_meta = _load_json_dict(dest_file)
    src_meta = _load_json_dict(src_file)
    if not src_meta:
        _preserve_protected_conflict(
            dest_file.parent, dest_file, src_file.parent, src_file
        )
        return

    backup_path = dest_file.parent / f"metadata.from-{src_folder_name}.json"
    conflicts: dict[str, dict[str, object]] = {}
    for key, src_value in src_meta.items():
        if key not in dest_meta:
            dest_meta[key] = src_value
            continue
        if dest_meta[key] == src_value:
            continue
        conflicts[key] = {"kept": dest_meta[key], "alternate": src_value}

    backup_path.write_text(json.dumps(src_meta, indent=2), encoding="utf-8")
    if conflicts:
        merge_conflicts = dest_meta.setdefault("_metadata_merge_conflicts", {})
        if isinstance(merge_conflicts, dict):
            merge_conflicts[src_folder_name] = conflicts
    dest_file.write_text(json.dumps(dest_meta, indent=2), encoding="utf-8")


def _preserve_protected_conflict(
    dest_dir: Path,
    dest_file: Path,
    src_dir: Path,
    src_file: Path,
) -> None:
    backup = dest_dir / f"{src_file.stem}.from-{src_dir.name}{src_file.suffix}"
    if not backup.exists():
        shutil.copy2(src_file, backup)
    if not dest_file.read_text(encoding="utf-8").strip():
        shutil.copy2(src_file, dest_file)


def _assert_protected_contents_preserved(dest: Path, src: Path) -> None:
    for name in PROTECTED_ARTIFACTS:
        src_file = src / name
        if not src_file.is_file():
            continue
        if name == "metadata.json":
            if _metadata_fields_preserved(dest, src, src_file):
                continue
            raise RuntimeError(
                f"Refusing to remove {src}: metadata.json fields not preserved in {dest}"
            )
        src_bytes = src_file.read_bytes()
        dest_file = dest / name
        if dest_file.is_file() and dest_file.read_bytes() == src_bytes:
            continue
        backup = dest / f"{src_file.stem}.from-{src.name}{src_file.suffix}"
        if backup.is_file() and backup.read_bytes() == src_bytes:
            continue
        raise RuntimeError(
            f"Refusing to remove {src}: protected file {name} not preserved in {dest}"
        )


def _metadata_fields_preserved(dest: Path, src: Path, src_file: Path) -> bool:
    src_meta = _load_json_dict(src_file)
    if not src_meta:
        return True
    dest_meta = _load_json_dict(dest / "metadata.json")
    backup_metas = [_load_json_dict(path) for path in dest.glob("metadata.from-*.json")]
    conflict_blocks = dest_meta.get("_metadata_merge_conflicts")
    if not isinstance(conflict_blocks, dict):
        conflict_blocks = {}

    for key, src_value in src_meta.items():
        if dest_meta.get(key) == src_value:
            continue
        if any(backup.get(key) == src_value for backup in backup_metas):
            continue
        if any(backup == src_meta for backup in backup_metas):
            continue
        src_conflicts = conflict_blocks.get(src.name)
        if isinstance(src_conflicts, dict):
            entry = src_conflicts.get(key)
            if isinstance(entry, dict) and entry.get("alternate") == src_value:
                continue
        return False
    return True


def update_day_manifest_video_paths(
    day_dir: Path,
    video_id: str,
    folder_name: str,
) -> bool:
    manifest_path = day_dir / "manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    video_dir = day_dir / "videos" / folder_name
    changed = False
    for entry in manifest.get("videos") or []:
        if not isinstance(entry, dict) or entry.get("video_id") != video_id:
            continue
        if entry.get("archive_folder") != folder_name:
            entry["archive_folder"] = folder_name
            changed = True
        path_map = {
            "transcript_path": "transcript.md",
            "summary_path": "summary.md",
            "metadata_path": "metadata.json",
        }
        for key, filename in path_map.items():
            if key not in entry:
                continue
            updated = str(video_dir / filename)
            if entry.get(key) != updated:
                entry[key] = updated
                changed = True
    if changed:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return changed


def migrate_archive_video_folders(archive_root: Path, index: dict) -> int:
    """Consolidate video folders; update index and day manifests."""
    migrated = 0
    videos_index = index.setdefault("videos", {})
    for videos_dir in sorted(archive_root.glob("*/*/*/videos")):
        if not videos_dir.is_dir():
            continue
        day_dir = videos_dir.parent
        video_ids: set[str] = set()
        for child in videos_dir.iterdir():
            if not child.is_dir():
                continue
            video_id, _ = _read_folder_identity(child)
            if video_id:
                video_ids.add(video_id)

        for video_id in sorted(video_ids):
            title = _best_title_for_video(videos_dir, video_id, videos_index)
            before = [path.name for path in list_video_dirs(videos_dir, video_id)]
            folder_name = consolidate_video_folders(
                videos_dir, video_id=video_id, title=title
            )
            after = [path.name for path in list_video_dirs(videos_dir, video_id)]
            if before != after or (len(before) == 1 and before[0] != folder_name):
                migrated += 1
            entry = videos_index.setdefault(video_id, {})
            entry["archive_folder"] = folder_name
            if title:
                entry.setdefault("title", title)
            update_day_manifest_video_paths(day_dir, video_id, folder_name)
    return migrated


def _best_title_for_video(
    videos_dir: Path,
    video_id: str,
    videos_index: dict[str, dict],
) -> str:
    index_title = (videos_index.get(video_id) or {}).get("title")
    if index_title:
        return str(index_title)
    for folder in list_video_dirs(videos_dir, video_id):
        _, title = _read_folder_identity(folder)
        if title:
            return title
    return video_id


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
