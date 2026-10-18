"""Phase 2: sync Evals YouTube playlist into archive/responsiv/evals/."""

from __future__ import annotations

import json
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
from pipeline.archive_paths import build_video_folder_name, resolve_video_dir
from pipeline.archive_store import (
    load_index,
    maybe_push_to_github_archive,
    save_index,
    sync_archive_from_github,
    video_already_processed,
    write_video_artifacts,
)
from pipeline.config import PipelineConfig
from pipeline.git_archive import ArchiveGitError
from transcript_service import TranscriptResult, fetch_transcript
from youtube_utils import build_video_url

EVALS_PLAYLIST_ID = "PLOkQ3xh7s_rk"
EVALS_ARCHIVE_SUBPATH = Path("responsiv/evals")
PLAYLIST_INDEX_JSON = "playlist-index.json"
INDEX_MD = "index.md"


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
class EvalsPlaylistSyncResult:
    playlist_id: str
    playlist_title: str | None
    dry_run: bool
    archive_subpath: str
    videos: list[PlaylistVideoSyncEntry] = field(default_factory=list)
    extracted: int = 0
    referenced: int = 0
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
                "referenced": self.referenced,
                "skipped": self.skipped,
                "failed": self.failed,
                "playlist_videos": len(self.videos),
            },
            "archive_pull": self.archive_pull,
            "archive_push": self.archive_push,
            "videos": [entry.__dict__ for entry in self.videos],
        }


def evals_root(archive_root: Path) -> Path:
    return archive_root / EVALS_ARCHIVE_SUBPATH


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
        if existing.get(key):
            merged[key] = existing[key]
    return merged


def _is_transcript_eligible(detail: ApiVideoDetails | None) -> bool:
    if detail is None:
        return True
    if detail.upload_status and detail.upload_status != "processed":
        return False
    privacy = (detail.privacy_status or "public").lower()
    return privacy in ("public", "unlisted")


def locate_global_transcript_dir(
    archive_root: Path,
    video_id: str,
    index: dict[str, Any],
    *,
    exclude_prefix: Path | None = None,
) -> Path | None:
    """Find an existing daily-archive transcript folder for video_id."""
    entry = (index.get("videos") or {}).get(video_id) or {}
    folder_hint = entry.get("archive_folder")
    candidates: list[Path] = []
    for videos_dir in sorted(archive_root.glob("**/videos")):
        if not videos_dir.is_dir():
            continue
        if ".git-sync" in videos_dir.parts:
            continue
        if exclude_prefix is not None:
            try:
                videos_dir.relative_to(exclude_prefix)
                continue
            except ValueError:
                pass
        resolved = resolve_video_dir(videos_dir, video_id, folder_hint=folder_hint)
        if resolved is not None and (resolved / "transcript.md").is_file():
            candidates.append(resolved)
    if not candidates:
        return None
    return candidates[0]


def _evals_video_dir(evals_dir: Path, title: str, video_id: str) -> Path:
    folder = build_video_folder_name(title, video_id)
    return evals_dir / folder


def _read_metadata_fields(metadata_path: Path) -> dict[str, Any]:
    meta = _load_json_dict(metadata_path)
    published = meta.get("youtube_published_at_utc") or meta.get("published_at_utc")
    generated = meta.get("transcript_generated_at_utc")
    return {
        "youtube_published_at_utc": published,
        "transcript_generated_at_utc": generated,
        "transcript_provider": meta.get("transcript_provider"),
    }


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
    published_iso = ref.youtube_published_at_utc
    if detail is not None:
        published_iso = detail.published_at_utc.isoformat()
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
    archive_root: Path,
    evals_dir: Path,
    index: dict[str, Any],
    ref: PlaylistVideoRef,
    detail: ApiVideoDetails | None,
    prior: dict[str, Any],
) -> PlaylistVideoSyncEntry:
    """Classify a playlist row without downloading transcripts."""
    entry = _base_sync_entry(ref, detail=detail, prior=prior)
    if not _is_transcript_eligible(detail):
        return replace(entry, action="skipped", error="not_eligible")

    evals_video_path = _evals_video_dir(evals_dir, ref.title, ref.video_id)
    local_transcript = evals_video_path / "transcript.md"
    if local_transcript.is_file():
        meta_path = evals_video_path / "metadata.json"
        fields = _read_metadata_fields(meta_path) if meta_path.is_file() else {}
        return replace(
            entry,
            transcript_location=_relative_archive_path(archive_root, local_transcript),
            transcript_source="evals_archive",
            youtube_published_at_utc=(
                fields.get("youtube_published_at_utc") or entry.youtube_published_at_utc
            ),
            transcript_generated_at_utc=fields.get("transcript_generated_at_utc"),
            transcript_provider=fields.get("transcript_provider"),
            action="skipped",
        )

    global_dir = locate_global_transcript_dir(
        archive_root,
        ref.video_id,
        index,
        exclude_prefix=evals_dir,
    )
    if global_dir is not None and video_already_processed(index, ref.video_id):
        global_meta = global_dir / "metadata.json"
        fields = _read_metadata_fields(global_meta) if global_meta.is_file() else {}
        transcript_path = global_dir / "transcript.md"
        return replace(
            entry,
            transcript_location=_relative_archive_path(archive_root, transcript_path),
            transcript_source="global_archive",
            youtube_published_at_utc=(
                fields.get("youtube_published_at_utc") or entry.youtube_published_at_utc
            ),
            transcript_generated_at_utc=fields.get("transcript_generated_at_utc"),
            transcript_provider=fields.get("transcript_provider"),
            action="referenced",
        )

    return replace(entry, action="missing")


def _parse_published(ref: PlaylistVideoRef, detail: ApiVideoDetails | None) -> datetime:
    raw = ref.youtube_published_at_utc
    if detail is not None:
        return detail.published_at_utc
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


def _load_playlist_index(evals_dir: Path) -> dict[str, Any]:
    return _load_json_dict(evals_dir / PLAYLIST_INDEX_JSON)


def _prior_playlist_entries(evals_dir: Path) -> dict[str, dict[str, Any]]:
    payload = _load_playlist_index(evals_dir)
    out: dict[str, dict[str, Any]] = {}
    for row in payload.get("videos") or []:
        if isinstance(row, dict) and row.get("video_id"):
            out[str(row["video_id"])] = row
    return out


def write_playlist_index_artifacts(
    *,
    archive_root: Path,
    playlist_id: str,
    playlist_title: str | None,
    entries: list[PlaylistVideoSyncEntry],
    dry_run: bool,
) -> None:
    evals_dir = evals_root(archive_root)
    if dry_run:
        return
    evals_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "playlist_id": playlist_id,
        "playlist_title": playlist_title,
        "archive_subpath": EVALS_ARCHIVE_SUBPATH.as_posix(),
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
    (evals_dir / PLAYLIST_INDEX_JSON).write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    lines = [
        f"# {playlist_title or 'Evals playlist'}",
        "",
        f"Playlist ID: `{playlist_id}`",
        "",
        "| # | Title | YouTube | Transcript | Published (UTC) | Generated (UTC) | Provider |",
        "|---:|---|---|---|---|---|---|",
    ]
    for entry in sorted(entries, key=lambda item: item.position):
        loc = entry.transcript_location or "—"
        if loc != "—":
            loc = f"[{loc}]({loc})"
        lines.append(
            f"| {entry.position} | {entry.title} | {entry.youtube_url} | {loc} | "
            f"{entry.youtube_published_at_utc or '—'} | "
            f"{entry.transcript_generated_at_utc or '—'} | "
            f"{entry.transcript_provider or '—'} |"
        )
    (evals_dir / INDEX_MD).write_text("\n".join(lines) + "\n", encoding="utf-8")


def sync_evals_playlist(
    config: PipelineConfig,
    *,
    playlist: str = EVALS_PLAYLIST_ID,
    dry_run: bool = True,
    push: bool = False,
    pull: bool = True,
    video_id: str | None = None,
    fetch_transcript_fn: Callable[[str], TranscriptResult] = fetch_transcript,
) -> EvalsPlaylistSyncResult:
    playlist_id = parse_playlist_input(playlist)
    if not config.youtube_api_key:
        raise ValueError("YOUTUBE_API_KEY is required.")

    archive_root = config.archive_root
    evals_dir = evals_root(archive_root)
    result = EvalsPlaylistSyncResult(
        playlist_id=playlist_id,
        playlist_title=None,
        dry_run=dry_run,
        archive_subpath=EVALS_ARCHIVE_SUBPATH.as_posix(),
    )

    if pull and not dry_run:
        try:
            result.archive_pull = sync_archive_from_github(
                archive_root=archive_root,
                repo=config.archive_github_repo,
                token=config.archive_github_token,
            )
        except ArchiveGitError as exc:
            raise RuntimeError(str(exc)) from exc
    elif pull and dry_run and config.archive_github_repo and config.archive_github_token:
        try:
            result.archive_pull = sync_archive_from_github(
                archive_root=archive_root,
                repo=config.archive_github_repo,
                token=config.archive_github_token,
            )
        except ArchiveGitError as exc:
            raise RuntimeError(str(exc)) from exc

    index = load_index(archive_root)
    prior_rows = _prior_playlist_entries(evals_dir)
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
                archive_root=archive_root,
                evals_dir=evals_dir,
                index=index,
                ref=ref,
                detail=detail,
                prior=prior,
            )
            if entry.action == "referenced":
                result.referenced += 1
            elif entry.action == "skipped":
                result.skipped += 1
            sync_entries.append(entry)
            continue

        entry = resolve_playlist_video_entry(
            archive_root=archive_root,
            evals_dir=evals_dir,
            index=index,
            ref=ref,
            detail=detail,
            prior=prior,
        )
        if entry.action in ("skipped", "referenced"):
            if entry.action == "referenced":
                result.referenced += 1
            else:
                result.skipped += 1
            sync_entries.append(entry)
            continue

        youtube_url = entry.youtube_url
        published_iso = entry.youtube_published_at_utc
        evals_video_path = _evals_video_dir(evals_dir, ref.title, ref.video_id)

        if dry_run:
            sync_entries.append(replace(entry, action="would_extract"))
            continue

        transcript = fetch_transcript_fn(ref.video_id)
        if not transcript.available:
            result.failed += 1
            sync_entries.append(
                replace(
                    entry,
                    action="failed",
                    error=transcript.error_message or "transcript_unavailable",
                )
            )
            record = _discovery_record(
                video_id=ref.video_id,
                title=ref.title,
                youtube_url=youtube_url,
                published_at_utc=_parse_published(ref, detail),
            )
            upsert_video_index_entry(index, record, transcript, success=False)
            continue

        generated_at = _utc_now_iso()
        metadata = {
            "video_id": ref.video_id,
            "title": ref.title,
            "youtube_url": youtube_url,
            "youtube_published_at_utc": published_iso,
            "transcript_generated_at_utc": generated_at,
            "transcript_provider": transcript.provider,
        }
        existing_meta_path = evals_video_path / "metadata.json"
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
        evals_dir.mkdir(parents=True, exist_ok=True)
        folder_name = write_video_artifacts(
            videos_dir=evals_dir,
            video_id=ref.video_id,
            metadata=metadata,
            transcript_md=transcript_md,
            summary_md=None,
        ).archive_folder
        transcript_path = evals_dir / str(folder_name) / "transcript.md"
        entry = replace(
            entry,
            transcript_location=_relative_archive_path(archive_root, transcript_path),
            transcript_source="evals_archive",
            transcript_generated_at_utc=metadata.get("transcript_generated_at_utc"),
            transcript_provider=metadata.get("transcript_provider"),
            action="extracted",
        )
        result.extracted += 1

        record = _discovery_record(
            video_id=ref.video_id,
            title=ref.title,
            youtube_url=youtube_url,
            published_at_utc=_parse_published(ref, detail),
        )
        upsert_video_index_entry(index, record, transcript, success=True)

        sync_entries.append(entry)

    result.videos = sync_entries
    if not dry_run:
        save_index(archive_root, index)
        write_playlist_index_artifacts(
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
    else:
        write_playlist_index_artifacts(
            archive_root=archive_root,
            playlist_id=playlist_id,
            playlist_title=playlist_title,
            entries=sync_entries,
            dry_run=True,
        )

    return result
