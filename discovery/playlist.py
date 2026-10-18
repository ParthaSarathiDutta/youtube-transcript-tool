"""YouTube playlist discovery and archive gap reporting."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from discovery.youtube_data_api import ApiVideoDetails, YouTubeDataApiClient
from pipeline.archive_store import video_already_processed
from youtube_utils import UrlType, build_video_url, parse_youtube_url

_PLAYLIST_ID_PATTERN = re.compile(r"^[\w-]+$")


@dataclass(frozen=True)
class PlaylistVideoRef:
    video_id: str
    title: str
    position: int
    youtube_published_at_utc: str | None = None


@dataclass(frozen=True)
class PlaylistAuditReport:
    playlist_id: str
    playlist_title: str | None
    already_archived: list[PlaylistVideoRef] = field(default_factory=list)
    missing_transcripts: list[PlaylistVideoRef] = field(default_factory=list)
    unavailable: list[PlaylistVideoRef] = field(default_factory=list)

    def to_dict(self) -> dict:
        def rows(items: list[PlaylistVideoRef]) -> list[dict]:
            return [
                {
                    "video_id": item.video_id,
                    "title": item.title,
                    "youtube_url": build_video_url(item.video_id),
                    "position": item.position,
                    "youtube_published_at_utc": item.youtube_published_at_utc,
                }
                for item in items
            ]

        return {
            "playlist_id": self.playlist_id,
            "playlist_title": self.playlist_title,
            "counts": {
                "playlist_videos": (
                    len(self.already_archived)
                    + len(self.missing_transcripts)
                    + len(self.unavailable)
                ),
                "already_archived": len(self.already_archived),
                "missing_transcripts": len(self.missing_transcripts),
                "unavailable": len(self.unavailable),
            },
            "already_archived": rows(self.already_archived),
            "missing_transcripts": rows(self.missing_transcripts),
            "unavailable": rows(self.unavailable),
        }


def parse_playlist_input(value: str) -> str:
    """Accept a playlist URL or raw playlist ID."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Playlist URL or ID is required.")
    if raw.startswith("http://") or raw.startswith("https://"):
        url_type, playlist_id = parse_youtube_url(raw)
        if url_type != UrlType.PLAYLIST or not playlist_id:
            raise ValueError(f"Not a valid YouTube playlist URL: {value}")
        return playlist_id
    if "list=" in raw:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        playlist_id = parse_qs(parsed.query).get("list", [None])[0]
        if playlist_id:
            return playlist_id
    if not _PLAYLIST_ID_PATTERN.match(raw):
        raise ValueError(f"Invalid playlist ID: {value}")
    return raw


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def list_playlist_videos(
    client: YouTubeDataApiClient,
    playlist_id: str,
) -> tuple[str | None, list[PlaylistVideoRef]]:
    """Return playlist title and deduplicated videos in playlist order."""
    playlist_title = client.get_playlist_title(playlist_id)
    refs: list[PlaylistVideoRef] = []
    seen: set[str] = set()
    position = 0
    for video_id, title, playlist_published in client.iter_playlist_video_entries(
        playlist_id
    ):
        if video_id in seen:
            continue
        seen.add(video_id)
        position += 1
        refs.append(
            PlaylistVideoRef(
                video_id=video_id,
                title=title or video_id,
                position=position,
                youtube_published_at_utc=_iso_utc(playlist_published),
            )
        )
    return playlist_title, refs


def _with_youtube_published(
    ref: PlaylistVideoRef,
    detail: ApiVideoDetails | None,
) -> PlaylistVideoRef:
    published = _iso_utc(detail.published_at_utc) if detail else ref.youtube_published_at_utc
    return PlaylistVideoRef(
        video_id=ref.video_id,
        title=ref.title,
        position=ref.position,
        youtube_published_at_utc=published,
    )


def audit_playlist_against_archive(
    client: YouTubeDataApiClient,
    playlist_id: str,
    index: dict,
) -> PlaylistAuditReport:
    """
    Compare playlist videos to archive index (read-only).

    - already_archived: transcript_status success in index
    - missing_transcripts: on playlist, not successfully archived
    - unavailable: playlist entry not returned by videos.list (deleted/private)

    Phase 2 index fields (not written by this audit): ``transcript_generated_at_utc``
    is set only when the daily pipeline first stores a transcript; publish times
    use ``youtube_published_at_utc`` and are preserved on later syncs.
    """
    playlist_title, refs = list_playlist_videos(client, playlist_id)
    if not refs:
        return PlaylistAuditReport(
            playlist_id=playlist_id,
            playlist_title=playlist_title,
        )

    details = client.get_videos_details([ref.video_id for ref in refs])
    already_archived: list[PlaylistVideoRef] = []
    missing_transcripts: list[PlaylistVideoRef] = []
    unavailable: list[PlaylistVideoRef] = []

    for ref in refs:
        detail = details.get(ref.video_id)
        enriched = _with_youtube_published(ref, detail)
        if ref.video_id not in details:
            unavailable.append(enriched)
            continue
        assert detail is not None
        if detail.privacy_status and detail.privacy_status != "public":
            unavailable.append(enriched)
            continue
        if detail.upload_status and detail.upload_status != "processed":
            unavailable.append(enriched)
            continue
        if video_already_processed(index, ref.video_id):
            already_archived.append(enriched)
        else:
            missing_transcripts.append(enriched)

    return PlaylistAuditReport(
        playlist_id=playlist_id,
        playlist_title=playlist_title,
        already_archived=already_archived,
        missing_transcripts=missing_transcripts,
        unavailable=unavailable,
    )
