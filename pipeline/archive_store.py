"""Local archive layout and idempotency index."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from document_writer import DailyVideoDocument, write_daily_combined_document
from pipeline.archive_paths import migrate_video_folder, resolve_video_dir
from pipeline.git_archive import (
    ArchiveGitError,
    clone_archive_repo,
    copy_remote_tree_into_archive,
    push_archive_repo,
    sanitize_git_message,
    archive_git_env,
)
from transcript_service import TranscriptResult


@dataclass
class VideoArchiveEntry:
    video_id: str
    title: str
    youtube_url: str
    published_at_utc: str | None
    discovery_category: str
    transcript_status: str
    transcript_provider: str | None
    summary_path: str | None
    transcript_path: str | None
    metadata_path: str
    archive_folder: str | None = None


def digest_day_paths(archive_root: Path, digest_date: date) -> dict[str, Path]:
    day_dir = (
        archive_root
        / str(digest_date.year)
        / f"{digest_date.month:02d}"
        / f"{digest_date.day:02d}"
    )
    return {
        "day_dir": day_dir,
        "videos_dir": day_dir / "videos",
        "manifest": day_dir / "manifest.json",
        "daily_summary": day_dir / "daily-summary.md",
        "combined_docx": day_dir / "transcripts-combined.docx",
        "email_preview_txt": day_dir / "email-preview.txt",
        "email_preview_html": day_dir / "email-preview.html",
    }


def load_index(archive_root: Path) -> dict[str, Any]:
    index_path = archive_root / "index.json"
    if not index_path.exists():
        return {"videos": {}}
    return json.loads(index_path.read_text(encoding="utf-8"))


def save_index(archive_root: Path, index: dict[str, Any]) -> None:
    archive_root.mkdir(parents=True, exist_ok=True)
    index_path = archive_root / "index.json"
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")


def video_already_processed(index: dict[str, Any], video_id: str) -> bool:
    entry = (index.get("videos") or {}).get(video_id) or {}
    return entry.get("transcript_status") == "success"


def video_digest_email_sent(index: dict[str, Any], video_id: str) -> bool:
    entry = (index.get("videos") or {}).get(video_id) or {}
    return bool(entry.get("digest_email_sent"))


def mark_videos_digest_emailed(index: dict[str, Any], video_ids: list[str]) -> None:
    videos = index.setdefault("videos", {})
    stamped = datetime.now(timezone.utc).isoformat()
    for video_id in video_ids:
        entry = videos.setdefault(video_id, {})
        entry["digest_email_sent"] = True
        entry["digest_email_sent_at_utc"] = stamped


def sync_archive_from_github(
    *,
    archive_root: Path,
    repo: str | None,
    token: str | None,
) -> str | None:
    """
    Load the private archive at run start (including index.json) by shallow-cloning
    and merging into archive_root.
    """
    if not repo or not token:
        return None
    work_dir = archive_root / ".git-sync"
    try:
        clone_archive_repo(repo, work_dir, token)
        copy_remote_tree_into_archive(work_dir, archive_root)
    except ArchiveGitError as exc:
        raise ArchiveGitError(f"Archive pull failed: {exc}") from exc
    return "pulled"


@dataclass(frozen=True)
class HydratedVideo:
    doc_item: DailyVideoDocument
    email_section: str
    manifest_entry: dict[str, Any]


def load_hydrated_video(
    videos_dir: Path,
    video_id: str,
    *,
    folder_hint: str | None = None,
) -> HydratedVideo | None:
    video_dir = resolve_video_dir(videos_dir, video_id, folder_hint=folder_hint)
    if video_dir is None:
        return None
    metadata_path = video_dir / "metadata.json"
    transcript_path = video_dir / "transcript.md"
    summary_path = video_dir / "summary.md"
    if not metadata_path.exists() or not transcript_path.exists():
        return None

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    transcript_raw = transcript_path.read_text(encoding="utf-8")
    summary_md = (
        summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    )
    display_text = _transcript_display_text(transcript_raw)
    if not display_text.strip():
        return None

    published_at: datetime | None = None
    published_raw = metadata.get("published_at_utc")
    if published_raw:
        published_at = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))

    transcript = TranscriptResult(
        segments=[],
        language_code=str(metadata.get("caption_language") or "en"),
        is_generated=bool(metadata.get("captions_generated")),
        display_text=display_text,
        available=True,
        provider=metadata.get("transcript_provider"),
    )
    title = str(metadata.get("title") or video_id)
    url = str(metadata.get("youtube_url") or f"https://www.youtube.com/watch?v={video_id}")
    doc_item = DailyVideoDocument(
        video_id=video_id,
        title=title,
        url=url,
        published_at_utc=published_at,
        transcript=transcript,
    )
    summary_enabled = metadata.get("summary_enabled", True)
    email_section = _email_section_text(
        title=title,
        url=url,
        provider=transcript.provider,
        summary_md=summary_md,
        summary_enabled=bool(summary_enabled),
    )
    manifest_entry = {
        "video_id": video_id,
        "title": title,
        "youtube_url": url,
        "published_at_utc": published_raw,
        "discovery_category": metadata.get("discovery_category", ""),
        "transcript_status": metadata.get("transcript_status", "success"),
        "transcript_provider": metadata.get("transcript_provider"),
        "archive_folder": video_dir.name,
        "summary_path": str(summary_path),
        "transcript_path": str(transcript_path),
        "metadata_path": str(metadata_path),
    }
    return HydratedVideo(
        doc_item=doc_item,
        email_section=email_section,
        manifest_entry=manifest_entry,
    )


def _email_section_text(
    *,
    title: str,
    url: str,
    provider: str | None,
    summary_md: str,
    summary_enabled: bool,
) -> str:
    lines = [
        f"VIDEO: {title}",
        f"YouTube: {url}",
        f"Transcript provider: {provider or 'unknown'}",
    ]
    if summary_enabled and summary_md.strip():
        lines.extend(["", summary_md.strip()])
    return "\n".join(lines) + "\n"


def _transcript_display_text(transcript_md: str) -> str:
    parts = transcript_md.split("\n\n", 2)
    if len(parts) >= 3 and parts[1].startswith("URL:"):
        return parts[2].strip()
    if len(parts) >= 2 and parts[0].startswith("#"):
        return "\n\n".join(parts[1:]).strip()
    return transcript_md.strip()


def write_video_artifacts(
    *,
    videos_dir: Path,
    video_id: str,
    metadata: dict[str, Any],
    transcript_md: str,
    summary_md: str | None = None,
) -> VideoArchiveEntry:
    videos_dir.mkdir(parents=True, exist_ok=True)
    title = str(metadata.get("title") or video_id)
    folder_name = migrate_video_folder(
        videos_dir, video_id=video_id, title=title
    )
    video_dir = videos_dir / folder_name
    video_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = video_dir / "transcript.md"
    summary_path = video_dir / "summary.md"
    metadata_path = video_dir / "metadata.json"
    transcript_path.write_text(transcript_md, encoding="utf-8")
    summary_path_str: str | None = None
    if summary_md is not None:
        summary_path.write_text(summary_md, encoding="utf-8")
        summary_path_str = str(summary_path)
    metadata = {**metadata, "archive_folder": folder_name}
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return VideoArchiveEntry(
        video_id=video_id,
        title=metadata.get("title", video_id),
        youtube_url=metadata.get("youtube_url", ""),
        published_at_utc=metadata.get("published_at_utc"),
        discovery_category=metadata.get("discovery_category", ""),
        transcript_status=metadata.get("transcript_status", "unknown"),
        transcript_provider=metadata.get("transcript_provider"),
        summary_path=summary_path_str,
        transcript_path=str(transcript_path),
        metadata_path=str(metadata_path),
        archive_folder=folder_name,
    )


def write_daily_bundle(
    *,
    archive_root: Path,
    digest_date: date,
    digest_title: str,
    daily_summary_md: str,
    doc_items: list[DailyVideoDocument],
    manifest: dict[str, Any],
) -> Path:
    paths = digest_day_paths(archive_root, digest_date)
    paths["day_dir"].mkdir(parents=True, exist_ok=True)
    paths["videos_dir"].mkdir(parents=True, exist_ok=True)
    paths["daily_summary"].write_text(daily_summary_md, encoding="utf-8")
    write_daily_combined_document(digest_title, doc_items, paths["combined_docx"])
    paths["manifest"].write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return paths["day_dir"]


def maybe_push_to_github_archive(
    *,
    archive_root: Path,
    repo: str | None,
    token: str | None,
) -> str | None:
    if not repo or not token:
        return None
    work_dir = archive_root / ".git-sync"
    try:
        clone_archive_repo(repo, work_dir, token)
        for item in archive_root.iterdir():
            if item.name == ".git-sync":
                continue
            dest = work_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        git_env = archive_git_env(token)
        subprocess.run(
            ["git", "add", "-A"],
            cwd=work_dir,
            env=git_env,
            capture_output=True,
            check=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=work_dir,
            env=git_env,
            capture_output=True,
            text=True,
            check=True,
        )
        if not status.stdout.strip():
            return "no_changes"
        msg = f"Daily archive sync {datetime.now(timezone.utc).isoformat()}"
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=work_dir,
            env=git_env,
            capture_output=True,
            check=True,
        )
        push_archive_repo(work_dir, token)
    except (ArchiveGitError, subprocess.CalledProcessError) as exc:
        stderr = ""
        if isinstance(exc, subprocess.CalledProcessError):
            stderr = sanitize_git_message(exc.stderr.decode() if exc.stderr else "")
        else:
            stderr = sanitize_git_message(str(exc))
        raise ArchiveGitError(f"Archive push failed: {stderr}") from exc
    return "pushed"
