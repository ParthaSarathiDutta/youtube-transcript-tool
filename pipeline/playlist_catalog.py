"""Registered YouTube playlist sync destinations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline.archive_index import (
    TRANSCRIPT_SCOPE_PLAYLIST,
    TRANSCRIPT_SCOPE_SYNTHETIC_DATA,
)

PLAYLIST_INDEX_JSON = "playlist-index.json"
INDEX_MD = "index.md"


@dataclass(frozen=True)
class PlaylistSyncTarget:
    """Configuration for syncing one curated playlist into the archive."""

    key: str
    default_playlist_id: str
    archive_subpath: Path
    transcript_scope: str
    transcript_source_label: str
    default_playlist_url: str | None = None

    @property
    def archive_subpath_posix(self) -> str:
        return self.archive_subpath.as_posix()

    def default_playlist(self) -> str:
        if self.default_playlist_url:
            return self.default_playlist_url
        return f"https://www.youtube.com/playlist?list={self.default_playlist_id}"


EVALS_PLAYLIST_TARGET = PlaylistSyncTarget(
    key="evals",
    default_playlist_id="PLOkQ3xh7s_rk",
    archive_subpath=Path("responsiv/evals"),
    transcript_scope=TRANSCRIPT_SCOPE_PLAYLIST,
    transcript_source_label="evals_archive",
)

SYNTHETIC_DATA_PLAYLIST_TARGET = PlaylistSyncTarget(
    key="synthetic-data",
    default_playlist_id="PLAeHHACevH9k",
    archive_subpath=Path("responsiv/synthetic data"),
    transcript_scope=TRANSCRIPT_SCOPE_SYNTHETIC_DATA,
    transcript_source_label="synthetic_data_archive",
    default_playlist_url="https://youtube.com/playlist?list=PLAeHHACevH9k",
)

PLAYLIST_SYNC_TARGETS: dict[str, PlaylistSyncTarget] = {
    EVALS_PLAYLIST_TARGET.key: EVALS_PLAYLIST_TARGET,
    SYNTHETIC_DATA_PLAYLIST_TARGET.key: SYNTHETIC_DATA_PLAYLIST_TARGET,
}


def get_playlist_sync_target(key: str) -> PlaylistSyncTarget:
    normalized = (key or "").strip().lower()
    if normalized in PLAYLIST_SYNC_TARGETS:
        return PLAYLIST_SYNC_TARGETS[normalized]
    raise ValueError(
        f"Unknown playlist sync target {key!r}. "
        f"Choose from: {', '.join(sorted(PLAYLIST_SYNC_TARGETS))}."
    )
