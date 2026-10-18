"""Thin YouTube Data API v3 client (injectable HTTP for tests)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

API_ROOT = "https://www.googleapis.com/youtube/v3"


class HttpGet(Protocol):
    def __call__(self, url: str, timeout: float) -> dict[str, Any]: ...


class YouTubeApiError(Exception):
    """YouTube Data API request failed."""

    def __init__(self, message: str, *, status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class ApiVideoDetails:
    video_id: str
    channel_id: str
    title: str
    published_at_utc: datetime
    privacy_status: str | None
    upload_status: str | None
    live_broadcast_content: str | None
    duration_seconds: int | None


def default_http_get(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        reason = None
        try:
            parsed = json.loads(body)
            reason = parsed.get("error", {}).get("message")
        except json.JSONDecodeError:
            reason = body[:200] or None
        raise YouTubeApiError(
            f"YouTube API HTTP {exc.code}: {reason or exc.reason}",
            status=exc.code,
            reason=reason,
        ) from exc
    except TimeoutError as exc:
        raise YouTubeApiError(f"YouTube API timeout after {timeout}s") from exc
    except urllib.error.URLError as exc:
        raise YouTubeApiError(f"YouTube API network error: {exc.reason}") from exc

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise YouTubeApiError("YouTube API returned invalid JSON") from exc


def parse_rfc3339_utc(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


_ISO8601_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_iso8601_duration_seconds(duration: str | None) -> int | None:
    if not duration:
        return None
    match = _ISO8601_DURATION.match(duration)
    if not match:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


class YouTubeDataApiClient:
    def __init__(
        self,
        api_key: str,
        *,
        http_get: HttpGet | None = None,
        request_timeout_seconds: float = 30.0,
    ):
        if not api_key:
            raise ValueError("YouTube Data API key is required.")
        self._api_key = api_key
        self._http_get = http_get or default_http_get
        self._timeout = request_timeout_seconds

    def _get(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        query = urllib.parse.urlencode({**params, "key": self._api_key})
        url = f"{API_ROOT}/{path}?{query}"
        try:
            return self._http_get(url, self._timeout)
        except TimeoutError as exc:
            raise YouTubeApiError(
                f"YouTube API timeout after {self._timeout}s"
            ) from exc

    def resolve_channel_id_for_handle(self, handle: str) -> str:
        normalized = handle.strip().lstrip("@")
        if not normalized:
            raise ValueError("Channel handle must not be empty.")
        data = self._get(
            "channels",
            {"part": "id", "forHandle": normalized},
        )
        items = data.get("items") or []
        if not items:
            raise YouTubeApiError(
                f"No channel found for handle @{normalized}",
                status=404,
            )
        return items[0]["id"]

    def get_uploads_playlist_id(self, channel_id: str) -> str:
        data = self._get(
            "channels",
            {"part": "contentDetails", "id": channel_id},
        )
        items = data.get("items") or []
        if not items:
            raise YouTubeApiError(f"Channel not found: {channel_id}", status=404)
        uploads = (
            items[0]
            .get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("uploads")
        )
        if not uploads:
            raise YouTubeApiError(
                f"Uploads playlist missing for channel {channel_id}",
            )
        return uploads

    def iter_recent_upload_playlist_entries(
        self,
        uploads_playlist_id: str,
        *,
        stop_before_utc: datetime,
        max_pages: int = 100,
    ):
        """
        Yield (video_id, videoPublishedAt UTC) from the uploads playlist (newest
        first). Stop paginating when every item on a page has videoPublishedAt
        before ``stop_before_utc``.
        """
        page_token: str | None = None
        pages = 0
        while pages < max_pages:
            pages += 1
            params: dict[str, str | int] = {
                "part": "contentDetails",
                "playlistId": uploads_playlist_id,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token

            data = self._get("playlistItems", params)
            items = data.get("items") or []
            if not items:
                break

            page_has_in_range = False
            for item in items:
                content = item.get("contentDetails") or {}
                video_id = content.get("videoId")
                if not video_id:
                    continue
                published_raw = content.get("videoPublishedAt")
                if published_raw:
                    published = parse_rfc3339_utc(published_raw)
                    if published >= stop_before_utc:
                        page_has_in_range = True
                        yield video_id, published
                else:
                    page_has_in_range = True
                    yield video_id, None

            if not page_has_in_range:
                break

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    def get_playlist_title(self, playlist_id: str) -> str | None:
        data = self._get(
            "playlists",
            {"part": "snippet", "id": playlist_id},
        )
        items = data.get("items") or []
        if not items:
            return None
        snippet = items[0].get("snippet") or {}
        return snippet.get("title") or playlist_id

    def iter_playlist_video_entries(
        self,
        playlist_id: str,
        *,
        max_pages: int = 100,
    ):
        """
        Yield (video_id, title, playlist_published_at_utc) for each playlist item.

        ``playlist_published_at_utc`` comes from playlistItems contentDetails
        (videoPublishedAt). Authoritative YouTube upload time is still from
        videos.list (see audit enrichment).
        """
        page_token: str | None = None
        pages = 0
        while pages < max_pages:
            pages += 1
            params: dict[str, str | int] = {
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token

            data = self._get("playlistItems", params)
            items = data.get("items") or []
            if not items:
                break

            for item in items:
                content = item.get("contentDetails") or {}
                video_id = content.get("videoId")
                if not video_id:
                    continue
                snippet = item.get("snippet") or {}
                title = snippet.get("title") or video_id
                published_raw = content.get("videoPublishedAt")
                published = (
                    parse_rfc3339_utc(published_raw) if published_raw else None
                )
                yield video_id, title, published

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    def get_videos_details(self, video_ids: list[str]) -> dict[str, ApiVideoDetails]:
        if not video_ids:
            return {}
        result: dict[str, ApiVideoDetails] = {}
        for start in range(0, len(video_ids), 50):
            batch = video_ids[start : start + 50]
            data = self._get(
                "videos",
                {
                    "part": "snippet,contentDetails,status",
                    "id": ",".join(batch),
                },
            )
            for item in data.get("items") or []:
                parsed = self._parse_video_item(item)
                if parsed:
                    result[parsed.video_id] = parsed
        return result

    def _parse_video_item(self, item: dict[str, Any]) -> ApiVideoDetails | None:
        video_id = item.get("id")
        if not video_id:
            return None
        snippet = item.get("snippet") or {}
        status = item.get("status") or {}
        content = item.get("contentDetails") or {}
        published_raw = snippet.get("publishedAt")
        if not published_raw:
            return None
        return ApiVideoDetails(
            video_id=video_id,
            channel_id=snippet.get("channelId") or "",
            title=snippet.get("title") or video_id,
            published_at_utc=parse_rfc3339_utc(published_raw),
            privacy_status=status.get("privacyStatus"),
            upload_status=status.get("uploadStatus"),
            live_broadcast_content=snippet.get("liveBroadcastContent"),
            duration_seconds=parse_iso8601_duration_seconds(content.get("duration")),
        )
