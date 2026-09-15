"""YouTube URL parsing, validation, and metadata retrieval."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, urlparse

import yt_dlp


class UrlType(str, Enum):
    VIDEO = "video"
    PLAYLIST = "playlist"
    INVALID = "invalid"


@dataclass(frozen=True)
class VideoMetadata:
    video_id: str
    title: str
    url: str


@dataclass(frozen=True)
class PlaylistMetadata:
    playlist_id: str
    title: str
    url: str
    videos: list[VideoMetadata]


_VIDEO_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{11}$")
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def parse_youtube_url(url: str) -> tuple[UrlType, str | None]:
    """
    Detect URL type and return (type, id).
    For videos, id is the video ID. For playlists, id is the playlist ID.
    """
    url = (url or "").strip()
    if not url:
        return UrlType.INVALID, None

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return UrlType.INVALID, None

    host = (parsed.netloc or "").lower().removeprefix("www.")
    if host not in ("youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"):
        return UrlType.INVALID, None

    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
        if _VIDEO_ID_PATTERN.match(video_id or ""):
            return UrlType.VIDEO, video_id
        return UrlType.INVALID, None

    query = parse_qs(parsed.query)
    path = parsed.path or ""

    if "list" in query and path.startswith("/playlist"):
        playlist_id = query["list"][0]
        if playlist_id:
            return UrlType.PLAYLIST, playlist_id

    video_id = None
    if "v" in query:
        video_id = query["v"][0]
    elif path.startswith("/watch/") and len(path.split("/")) >= 3:
        video_id = path.split("/")[2].split("?")[0]
    elif path.startswith("/shorts/") and len(path.split("/")) >= 3:
        video_id = path.split("/")[2].split("?")[0]
    elif path.startswith("/embed/") and len(path.split("/")) >= 3:
        video_id = path.split("/")[2].split("?")[0]

    if video_id and _VIDEO_ID_PATTERN.match(video_id):
        if "list" in query and not path.startswith("/playlist"):
            return UrlType.VIDEO, video_id
        return UrlType.VIDEO, video_id

    if "list" in query:
        return UrlType.PLAYLIST, query["list"][0]

    return UrlType.INVALID, None


def build_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def build_playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def sanitize_filename(name: str, max_length: int = 120) -> str:
    cleaned = _INVALID_FILENAME_CHARS.sub("", name).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "youtube_transcript"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip()
    return cleaned


def fetch_video_metadata(video_id: str) -> VideoMetadata:
    url = build_video_url(video_id)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise ValueError(f"Could not retrieve video metadata: {exc}") from exc

    if not info:
        raise ValueError("Could not retrieve video metadata.")

    title = info.get("title") or video_id
    return VideoMetadata(video_id=video_id, title=title, url=url)


def fetch_playlist_metadata(playlist_id: str) -> PlaylistMetadata:
    """Retrieve playlist title and videos in playlist order (deduplicated by video ID)."""
    url = build_playlist_url(playlist_id)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise ValueError(f"Could not retrieve playlist metadata: {exc}") from exc

    if not info:
        raise ValueError("Could not retrieve playlist metadata.")

    playlist_title = info.get("title") or playlist_id
    entries = info.get("entries") or []

    videos: list[VideoMetadata] = []
    seen_ids: set[str] = set()

    for entry in entries:
        if not entry:
            continue
        video_id = entry.get("id") or entry.get("url", "").split("v=")[-1].split("&")[0]
        if not video_id or not _VIDEO_ID_PATTERN.match(video_id):
            continue
        if video_id in seen_ids:
            continue
        seen_ids.add(video_id)

        title = (entry.get("title") or "").strip()
        if not title or title == video_id:
            try:
                title = fetch_video_metadata(video_id).title
            except ValueError:
                pass
        if not title:
            title = video_id
        videos.append(
            VideoMetadata(
                video_id=video_id,
                title=title,
                url=build_video_url(video_id),
            )
        )

    return PlaylistMetadata(
        playlist_id=playlist_id,
        title=playlist_title,
        url=url,
        videos=videos,
    )
