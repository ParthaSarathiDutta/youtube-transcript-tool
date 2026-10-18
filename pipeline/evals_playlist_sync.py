"""Backward-compatible re-exports for Evals playlist sync."""

from __future__ import annotations

from pathlib import Path

from pipeline.playlist_catalog import (
    EVALS_PLAYLIST_TARGET,
    INDEX_MD,
    PLAYLIST_INDEX_JSON,
)
from pipeline.playlist_sync import (
    PlaylistSyncResult,
    PlaylistVideoSyncEntry,
    copy_daily_transcript_to_evals,
    is_valid_transcript_file,
    locate_global_transcript_dir,
    markdown_transcript_link,
    resolve_playlist_video_entry,
    sync_evals_playlist,
    sync_playlist,
    write_playlist_index_artifacts,
)

EVALS_PLAYLIST_ID = EVALS_PLAYLIST_TARGET.default_playlist_id
EVALS_ARCHIVE_SUBPATH = EVALS_PLAYLIST_TARGET.archive_subpath

EvalsPlaylistSyncResult = PlaylistSyncResult


def evals_root(archive_root: Path) -> Path:
    return archive_root / EVALS_ARCHIVE_SUBPATH


__all__ = [
    "EVALS_ARCHIVE_SUBPATH",
    "EVALS_PLAYLIST_ID",
    "EvalsPlaylistSyncResult",
    "INDEX_MD",
    "PLAYLIST_INDEX_JSON",
    "PlaylistVideoSyncEntry",
    "copy_daily_transcript_to_evals",
    "evals_root",
    "is_valid_transcript_file",
    "locate_global_transcript_dir",
    "markdown_transcript_link",
    "resolve_playlist_video_entry",
    "sync_evals_playlist",
    "sync_playlist",
    "write_playlist_index_artifacts",
]
