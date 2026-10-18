"""Sync curated YouTube playlists into archive/responsiv/* destinations."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from discovery.models import (
    DiscoveryCategory,
    DiscoveryRecord,
    DiscoveryStatus,
    VideoType,
)
from discovery.playlist import (
    PlaylistVideoRef,
    list_playlist_videos,
    parse_playlist_input,
)
from discovery.youtube_data_api import ApiVideoDetails, YouTubeDataApiClient
from pipeline.archive_index import upsert_video_index_entry
from pipeline.archive_paths import (
    build_video_folder_name,
    parse_video_id_from_folder,
    resolve_video_dir,
)
from pipeline.playlist_catalog import (
    INDEX_MD,
    PLAYLIST_INDEX_JSON,
    EVALS_PLAYLIST_TARGET,
    PlaylistSyncTarget,
)
from pipeline.archive_store import (
    load_index,
    maybe_push_to_github_archive,
    save_index,
    sync_archive_from_github,
    write_video_artifacts,
)
from pipeline.config import PipelineConfig
from pipeline.git_archive import ArchiveGitError
from transcript_service import TranscriptResult, fetch_transcript
from youtube_utils import build_video_url

@dataclass(frozen=True)
class PlaylistVideoSyncEntry:
    video_id: str
    title: str
    youtube_url: str
    position: int
    youtube_published_at_utc: str | None
    transcript_generated_at_utc: str | None = None
    transcript_provider: str | None = None
    transcript_location: str | None = None
    transcript_source: str | None = None
    action: str = "unchanged"
    error: str | None = None


@dataclass
class PlaylistSyncResult:
    playlist_id: str
    playlist_title: str | None
    dry_run: bool
    archive_subpath: str
    videos: list[PlaylistVideoSyncEntry] = field(default_factory=list)
    extracted: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    archive_pull: str | None = None
    archive_push: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "playlist_id": self.playlist_id,
            "playlist_title": self.playlist_title,
            "dry_run": self.dry_run,
            "archive_subpath": self.archive_subpath,
            "counts": {
                "extracted": self.extracted,
                "copied": self.copied,
                "skipped": self.skipped,
                "failed": self.failed,
                "playlist_videos": len(self.videos),
            },
            "archive_pull": self.archive_pull,
            "archive_push": self.archive_push,
            "videos": [entry.__dict__ for entry in self.videos],
        }


def playlist_dest_root(archive_root: Path, target: PlaylistSyncTarget) -> Path:
    return archive_root / target.archive_subpath


def _path_is_under(path: Path, prefix: Path) -> bool:
    try:
        path.resolve().relative_to(prefix.resolve())
        return True
    except ValueError:
        return False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative_archive_path(archive_root: Path, path: Path) -> str:
    return path.relative_to(archive_root).as_posix()


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _merge_metadata_preserve_timestamps(
    existing: dict[str, Any],
    updates: dict[str, Any],
) -> dict[str, Any]:
    merged = {**existing, **updates}
    for key in ("youtube_published_at_utc", "transcript_generated_at_utc"):
        if key in existing:
            merged[key] = existing[key]
    return merged


def _is_transcript_eligible(detail: ApiVideoDetails | None) -> bool:
    if detail is None:
        return True
    if detail.upload_status and detail.upload_status != "processed":
        return False
    privacy = (detail.privacy_status or "public").lower()
    return privacy in ("public", "unlisted")


def locate_daily_transcript_dir(
    archive_root: Path,
    video_id: str,
    index: dict[str, Any],
) -> Path | None:
    """Find a transcript under dated ``**/videos/**`` folders."""
    entry = (index.get("videos") or {}).get(video_id) or {}
    folder_hint = entry.get("archive_folder")
    candidates: list[Path] = []
    for videos_dir in sorted(archive_root.glob("**/videos")):
        if not videos_dir.is_dir():
            continue
        if ".git-sync" in videos_dir.parts:
            continue
        resolved = resolve_video_dir(videos_dir, video_id, folder_hint=folder_hint)
        if resolved is not None and is_valid_transcript_file(resolved / "transcript.md"):
            candidates.append(resolved)
    if not candidates:
        return None
    return candidates[0]


def locate_transcript_source_dir(
    archive_root: Path,
    video_id: str,
    index: dict[str, Any],
    *,
    exclude_dest: Path,
) -> Path | None:
    """Find an existing transcript in daily or other playlist archive folders."""
    daily = locate_daily_transcript_dir(archive_root, video_id, index)
    if daily is not None:
        return daily

    for transcript_path in sorted(archive_root.glob("responsiv/**/transcript.md")):
        if ".git-sync" in transcript_path.parts:
            continue
        video_dir = transcript_path.parent
        if _path_is_under(video_dir, exclude_dest):
            continue
        folder_video_id = parse_video_id_from_folder(video_dir.name)
        if folder_video_id != video_id:
            continue
        if is_valid_transcript_file(transcript_path):
            return video_dir
    return None


def locate_global_transcript_dir(
    archive_root: Path,
    video_id: str,
    index: dict[str, Any],
    *,
    exclude_prefix: Path | None = None,
) -> Path | None:
    """Backward-compatible alias (Evals tests): daily archive only."""
    _ = exclude_prefix
    return locate_daily_transcript_dir(archive_root, video_id, index)


def is_valid_transcript_file(path: Path) -> bool:
    return path.is_file() and bool(path.read_text(encoding="utf-8").strip())


def _playlist_video_dir(dest_dir: Path, title: str, video_id: str) -> Path:
    folder = build_video_folder_name(title, video_id)
    return dest_dir / folder


def _read_metadata_fields(metadata_path: Path) -> dict[str, Any]:
    meta = _load_json_dict(metadata_path)
    published = meta.get("youtube_published_at_utc") or meta.get("published_at_utc")
    generated = meta.get("transcript_generated_at_utc")
    return {
        "youtube_published_at_utc": published,
        "transcript_generated_at_utc": generated,
        "transcript_provider": meta.get("transcript_provider"),
    }


def _entry_from_playlist_folder(
    entry: PlaylistVideoSyncEntry,
    *,
    archive_root: Path,
    dest_video_path: Path,
    transcript_source_label: str,
) -> PlaylistVideoSyncEntry:
    transcript_path = dest_video_path / "transcript.md"
    meta_path = dest_video_path / "metadata.json"
    fields = _read_metadata_fields(meta_path) if meta_path.is_file() else {}
    return replace(
        entry,
        transcript_location=_relative_archive_path(archive_root, transcript_path),
        transcript_source=transcript_source_label,
        youtube_published_at_utc=(
            fields.get("youtube_published_at_utc") or entry.youtube_published_at_utc
        ),
        transcript_generated_at_utc=fields.get("transcript_generated_at_utc"),
        transcript_provider=fields.get("transcript_provider"),
        action="skipped",
    )


def _generation_time_from_sources(
    source_meta: dict[str, Any],
    index_entry: dict[str, Any],
) -> str | None:
    for candidate in (
        source_meta.get("transcript_generated_at_utc"),
        index_entry.get("transcript_generated_at_utc"),
    ):
        if candidate:
            return str(candidate)
    return None


def _build_playlist_metadata(
    *,
    video_id: str,
    title: str,
    youtube_url: str,
    youtube_published_at_utc: str | None,
    transcript_provider: str | None,
    transcript_generated_at_utc: str | None,
) -> dict[str, Any]:
    return {
        "video_id": video_id,
        "title": title,
        "youtube_url": youtube_url,
        "youtube_published_at_utc": youtube_published_at_utc,
        "transcript_generated_at_utc": transcript_generated_at_utc,
        "transcript_provider": transcript_provider,
    }


def copy_transcript_to_playlist_dest(
    *,
    target: PlaylistSyncTarget,
    archive_root: Path,
    dest_dir: Path,
    ref: PlaylistVideoRef,
    detail: ApiVideoDetails | None,
    source_dir: Path,
    index: dict[str, Any],
) -> PlaylistVideoSyncEntry:
    """Copy transcript.md + metadata into playlist destination without re-downloading."""
    youtube_url = build_video_url(ref.video_id)
    published_iso = _published_iso(ref, detail)
    dest_video_path = _playlist_video_dir(dest_dir, ref.title, ref.video_id)
    entry = PlaylistVideoSyncEntry(
        video_id=ref.video_id,
        title=ref.title,
        youtube_url=youtube_url,
        position=ref.position,
        youtube_published_at_utc=published_iso,
    )

    if is_valid_transcript_file(dest_video_path / "transcript.md"):
        return _entry_from_playlist_folder(
            entry,
            archive_root=archive_root,
            dest_video_path=dest_video_path,
            transcript_source_label=target.transcript_source_label,
        )

    source_meta_path = source_dir / "metadata.json"
    source_fields = (
        _read_metadata_fields(source_meta_path) if source_meta_path.is_file() else {}
    )
    index_entry = (index.get("videos") or {}).get(ref.video_id) or {}
    generated_at = _generation_time_from_sources(source_fields, index_entry)
    provider = source_fields.get("transcript_provider") or index_entry.get(
        "transcript_provider"
    )
    published_iso = published_iso or source_fields.get("youtube_published_at_utc")

    dest_video_path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_dir / "transcript.md", dest_video_path / "transcript.md")

    metadata = _build_playlist_metadata(
        video_id=ref.video_id,
        title=ref.title,
        youtube_url=youtube_url,
        youtube_published_at_utc=published_iso,
        transcript_provider=provider,
        transcript_generated_at_utc=generated_at,
    )
    existing_meta_path = dest_video_path / "metadata.json"
    if existing_meta_path.is_file():
        metadata = _merge_metadata_preserve_timestamps(
            _load_json_dict(existing_meta_path),
            metadata,
        )
    metadata["archive_folder"] = dest_video_path.name
    (dest_video_path / "metadata.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    transcript_path = dest_video_path / "transcript.md"
    return replace(
        entry,
        transcript_location=_relative_archive_path(archive_root, transcript_path),
        transcript_source=target.transcript_source_label,
        youtube_published_at_utc=metadata.get("youtube_published_at_utc"),
        transcript_generated_at_utc=metadata.get("transcript_generated_at_utc"),
        transcript_provider=metadata.get("transcript_provider"),
        action="copied",
    )


def copy_daily_transcript_to_evals(
    *,
    archive_root: Path,
    evals_dir: Path,
    ref: PlaylistVideoRef,
    detail: ApiVideoDetails | None,
    daily_dir: Path,
    index: dict[str, Any],
) -> PlaylistVideoSyncEntry:
    """Backward-compatible Evals helper."""
    return copy_transcript_to_playlist_dest(
        target=EVALS_PLAYLIST_TARGET,
        archive_root=archive_root,
        dest_dir=evals_dir,
        ref=ref,
        detail=detail,
        source_dir=daily_dir,
        index=index,
    )


def _discovery_record(
    *,
    video_id: str,
    title: str,
    youtube_url: str,
    published_at_utc: datetime,
) -> DiscoveryRecord:
    now = datetime.now(timezone.utc)
    return DiscoveryRecord(
        video_id=video_id,
        channel_id="",
        title=title,
        youtube_url=youtube_url,
        published_at_utc=published_at_utc,
        discovery_timestamp_utc=now,
        video_type=VideoType.STANDARD,
        status=DiscoveryStatus.DISCOVERED,
        discovery_category=DiscoveryCategory.MANUAL_VIDEO_ID,
    )


def _base_sync_entry(
    ref: PlaylistVideoRef,
    *,
    detail: ApiVideoDetails | None,
    prior: dict[str, Any],
) -> PlaylistVideoSyncEntry:
    youtube_url = build_video_url(ref.video_id)
    published_iso = _published_iso(ref, detail)
    return PlaylistVideoSyncEntry(
        video_id=ref.video_id,
        title=ref.title,
        youtube_url=youtube_url,
        position=ref.position,
        youtube_published_at_utc=published_iso,
        transcript_generated_at_utc=prior.get("transcript_generated_at_utc"),
        transcript_provider=prior.get("transcript_provider"),
        transcript_location=prior.get("transcript_location"),
        transcript_source=prior.get("transcript_source"),
    )


def resolve_playlist_video_entry(
    *,
    target: PlaylistSyncTarget,
    archive_root: Path,
    dest_dir: Path,
    index: dict[str, Any],
    ref: PlaylistVideoRef,
    detail: ApiVideoDetails | None,
    prior: dict[str, Any],
) -> PlaylistVideoSyncEntry:
    """Classify a playlist row without downloading or copying transcripts."""
    entry = _base_sync_entry(ref, detail=detail, prior=prior)
    if not _is_transcript_eligible(detail):
        return replace(entry, action="skipped", error="not_eligible")

    dest_video_path = _playlist_video_dir(dest_dir, ref.title, ref.video_id)
    if is_valid_transcript_file(dest_video_path / "transcript.md"):
        return _entry_from_playlist_folder(
            entry,
            archive_root=archive_root,
            dest_video_path=dest_video_path,
            transcript_source_label=target.transcript_source_label,
        )

    if locate_transcript_source_dir(
        archive_root, ref.video_id, index, exclude_dest=dest_dir
    ) is not None:
        return replace(entry, action="needs_copy")

    return replace(entry, action="needs_extract")


def _published_iso(
    ref: PlaylistVideoRef,
    detail: ApiVideoDetails | None,
) -> str | None:
    if detail is not None:
        return detail.published_at_utc.isoformat()
    return ref.youtube_published_at_utc


def _published_datetime_for_record(
    ref: PlaylistVideoRef,
    detail: ApiVideoDetails | None,
) -> datetime:
    """Placeholder for DiscoveryRecord when YouTube publish time is unknown."""
    raw = _published_iso(ref, detail)
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def markdown_transcript_link(
    *,
    dest_dir: Path,
    archive_root: Path,
    transcript_location: str,
) -> str:
    """GitHub-friendly relative link from playlist index.md to a transcript file."""
    target = (archive_root / transcript_location).resolve()
    href = Path(os.path.relpath(target, dest_dir.resolve())).as_posix()
    label = Path(transcript_location).as_posix()
    return f"[{label}]({href})"


def _load_playlist_index(dest_dir: Path) -> dict[str, Any]:
    return _load_json_dict(dest_dir / PLAYLIST_INDEX_JSON)


def _prior_playlist_entries(dest_dir: Path) -> dict[str, dict[str, Any]]:
    payload = _load_playlist_index(dest_dir)
    out: dict[str, dict[str, Any]] = {}
    for row in payload.get("videos") or []:
        if isinstance(row, dict) and row.get("video_id"):
            out[str(row["video_id"])] = row
    return out


def write_playlist_index_artifacts(
    *,
    target: PlaylistSyncTarget,
    archive_root: Path,
    playlist_id: str,
    playlist_title: str | None,
    entries: list[PlaylistVideoSyncEntry],
    dry_run: bool,
) -> None:
    dest_dir = playlist_dest_root(archive_root, target)
    if dry_run:
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "playlist_id": playlist_id,
        "playlist_title": playlist_title,
        "archive_subpath": target.archive_subpath_posix,
        "synced_at_utc": _utc_now_iso(),
        "videos": [
            {
                "video_id": entry.video_id,
                "title": entry.title,
                "youtube_url": entry.youtube_url,
                "position": entry.position,
                "youtube_published_at_utc": entry.youtube_published_at_utc,
                "transcript_generated_at_utc": entry.transcript_generated_at_utc,
                "transcript_provider": entry.transcript_provider,
                "transcript_location": entry.transcript_location,
                "transcript_source": entry.transcript_source,
            }
            for entry in sorted(entries, key=lambda item: item.position)
        ],
    }
    (dest_dir / PLAYLIST_INDEX_JSON).write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    lines = [
        f"# {playlist_title or target.key + ' playlist'}",
        "",
        f"Playlist ID: `{playlist_id}`",
        "",
        "| # | Title | YouTube | Transcript | Published (UTC) | Generated (UTC) | Provider |",
        "|---:|---|---|---|---|---|---|",
    ]
    for entry in sorted(entries, key=lambda item: item.position):
        loc = entry.transcript_location or "—"
        if loc != "—" and loc.startswith(target.archive_subpath_posix):
            loc = markdown_transcript_link(
                dest_dir=dest_dir,
                archive_root=archive_root,
                transcript_location=loc,
            )
        lines.append(
            f"| {entry.position} | {entry.title} | {entry.youtube_url} | {loc} | "
            f"{entry.youtube_published_at_utc or '—'} | "
            f"{entry.transcript_generated_at_utc or '—'} | "
            f"{entry.transcript_provider or '—'} |"
        )
    (dest_dir / INDEX_MD).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _process_playlist_video(
    *,
    target: PlaylistSyncTarget,
    archive_root: Path,
    dest_dir: Path,
    index: dict[str, Any],
    ref: PlaylistVideoRef,
    detail: ApiVideoDetails | None,
    prior: dict[str, Any],
    dry_run: bool,
    fetch_transcript_fn: Callable[[str], TranscriptResult],
) -> tuple[PlaylistVideoSyncEntry, str]:
    """Returns (entry, outcome) where outcome is extracted|copied|skipped|failed|planned_*."""
    entry = resolve_playlist_video_entry(
        target=target,
        archive_root=archive_root,
        dest_dir=dest_dir,
        index=index,
        ref=ref,
        detail=detail,
        prior=prior,
    )
    if entry.action == "skipped":
        return entry, "skipped"
    if entry.action == "needs_copy":
        if dry_run:
            return replace(entry, action="would_copy"), "would_copy"
        source_dir = locate_transcript_source_dir(
            archive_root,
            ref.video_id,
            index,
            exclude_dest=dest_dir,
        )
        assert source_dir is not None
        copied_entry = copy_transcript_to_playlist_dest(
            target=target,
            archive_root=archive_root,
            dest_dir=dest_dir,
            ref=ref,
            detail=detail,
            source_dir=source_dir,
            index=index,
        )
        return copied_entry, "copied"

    if entry.action != "needs_extract":
        return entry, entry.action

    if dry_run:
        return replace(entry, action="would_extract"), "would_extract"

    youtube_url = entry.youtube_url
    published_iso = _published_iso(ref, detail)
    dest_video_path = _playlist_video_dir(dest_dir, ref.title, ref.video_id)

    if is_valid_transcript_file(dest_video_path / "transcript.md"):
        return _entry_from_playlist_folder(
            entry,
            archive_root=archive_root,
            dest_video_path=dest_video_path,
            transcript_source_label=target.transcript_source_label,
        ), "skipped"

    transcript = fetch_transcript_fn(ref.video_id)
    if not transcript.available:
        record = _discovery_record(
            video_id=ref.video_id,
            title=ref.title,
            youtube_url=youtube_url,
            published_at_utc=_published_datetime_for_record(ref, detail),
        )
        upsert_video_index_entry(
            index,
            record,
            transcript,
            success=False,
            transcript_scope=target.transcript_scope,
        )
        return (
            replace(
                entry,
                action="failed",
                error=transcript.error_message or "transcript_unavailable",
            ),
            "failed",
        )

    generated_at = _utc_now_iso()
    metadata = _build_playlist_metadata(
        video_id=ref.video_id,
        title=ref.title,
        youtube_url=youtube_url,
        youtube_published_at_utc=published_iso,
        transcript_provider=transcript.provider,
        transcript_generated_at_utc=generated_at,
    )
    existing_meta_path = dest_video_path / "metadata.json"
    if existing_meta_path.is_file():
        metadata = _merge_metadata_preserve_timestamps(
            _load_json_dict(existing_meta_path),
            metadata,
        )

    transcript_md = (
        f"# {ref.title}\n\n"
        f"URL: {youtube_url}\n\n"
        f"{transcript.display_text}\n"
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    folder_name = write_video_artifacts(
        videos_dir=dest_dir,
        video_id=ref.video_id,
        metadata=metadata,
        transcript_md=transcript_md,
        summary_md=None,
    ).archive_folder
    transcript_path = dest_dir / str(folder_name) / "transcript.md"
    extracted_entry = replace(
        entry,
        transcript_location=_relative_archive_path(archive_root, transcript_path),
        transcript_source=target.transcript_source_label,
        transcript_generated_at_utc=metadata.get("transcript_generated_at_utc"),
        transcript_provider=metadata.get("transcript_provider"),
        action="extracted",
    )

    record = _discovery_record(
        video_id=ref.video_id,
        title=ref.title,
        youtube_url=youtube_url,
        published_at_utc=_published_datetime_for_record(ref, detail),
    )
    upsert_video_index_entry(
        index,
        record,
        transcript,
        success=True,
        transcript_scope=target.transcript_scope,
    )
    return extracted_entry, "extracted"


def sync_playlist(
    config: PipelineConfig,
    target: PlaylistSyncTarget,
    *,
    playlist: str | None = None,
    dry_run: bool = True,
    push: bool = False,
    pull: bool = True,
    video_id: str | None = None,
    fetch_transcript_fn: Callable[[str], TranscriptResult] = fetch_transcript,
) -> PlaylistSyncResult:
    playlist_input = playlist or target.default_playlist()
    playlist_id = parse_playlist_input(playlist_input)
    if not config.youtube_api_key:
        raise ValueError("YOUTUBE_API_KEY is required.")

    archive_root = config.archive_root
    dest_dir = playlist_dest_root(archive_root, target)
    result = PlaylistSyncResult(
        playlist_id=playlist_id,
        playlist_title=None,
        dry_run=dry_run,
        archive_subpath=target.archive_subpath_posix,
    )

    if pull:
        if not dry_run or (config.archive_github_repo and config.archive_github_token):
            try:
                result.archive_pull = sync_archive_from_github(
                    archive_root=archive_root,
                    repo=config.archive_github_repo,
                    token=config.archive_github_token,
                )
            except ArchiveGitError as exc:
                raise RuntimeError(str(exc)) from exc

    index = load_index(archive_root)
    prior_rows = _prior_playlist_entries(dest_dir)
    client = YouTubeDataApiClient(config.youtube_api_key)
    playlist_title, all_refs = list_playlist_videos(client, playlist_id)
    result.playlist_title = playlist_title

    if video_id and not any(ref.video_id == video_id for ref in all_refs):
        raise ValueError(f"Video {video_id} is not in playlist {playlist_id}.")

    process_ids = {video_id} if video_id else {ref.video_id for ref in all_refs}
    details = client.get_videos_details([ref.video_id for ref in all_refs])
    sync_entries: list[PlaylistVideoSyncEntry] = []

    for ref in all_refs:
        detail = details.get(ref.video_id)
        prior = prior_rows.get(ref.video_id, {})
        if ref.video_id not in process_ids:
            entry = resolve_playlist_video_entry(
                target=target,
                archive_root=archive_root,
                dest_dir=dest_dir,
                index=index,
                ref=ref,
                detail=detail,
                prior=prior,
            )
            sync_entries.append(entry)
            continue

        entry, outcome = _process_playlist_video(
            target=target,
            archive_root=archive_root,
            dest_dir=dest_dir,
            index=index,
            ref=ref,
            detail=detail,
            prior=prior,
            dry_run=dry_run,
            fetch_transcript_fn=fetch_transcript_fn,
        )
        if outcome == "extracted":
            result.extracted += 1
        elif outcome == "copied":
            result.copied += 1
        elif outcome == "skipped":
            result.skipped += 1
        elif outcome == "failed":
            result.failed += 1
        sync_entries.append(entry)

    result.videos = sync_entries
    if not dry_run:
        save_index(archive_root, index)
        write_playlist_index_artifacts(
            target=target,
            archive_root=archive_root,
            playlist_id=playlist_id,
            playlist_title=playlist_title,
            entries=sync_entries,
            dry_run=False,
        )
        if push:
            result.archive_push = maybe_push_to_github_archive(
                archive_root=archive_root,
                repo=config.archive_github_repo,
                token=config.archive_github_token,
            )

    return result


def sync_evals_playlist(
    config: PipelineConfig,
    *,
    playlist: str | None = None,
    dry_run: bool = True,
    push: bool = False,
    pull: bool = True,
    video_id: str | None = None,
    fetch_transcript_fn: Callable[[str], TranscriptResult] = fetch_transcript,
) -> PlaylistSyncResult:
    """Sync the Evals playlist (backward-compatible entry point)."""
    return sync_playlist(
        config,
        EVALS_PLAYLIST_TARGET,
        playlist=playlist,
        dry_run=dry_run,
        push=push,
        pull=pull,
        video_id=video_id,
        fetch_transcript_fn=fetch_transcript_fn,
    )
