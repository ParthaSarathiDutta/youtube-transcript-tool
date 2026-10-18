"""Playlist discovery pagination and archive deduplication."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from discovery.playlist import (
    audit_playlist_against_archive,
    list_playlist_videos,
    parse_playlist_input,
)
from discovery.youtube_data_api import ApiVideoDetails, YouTubeApiError, YouTubeDataApiClient
from datetime import datetime, timezone
from youtube_utils import PlaylistMetadata, VideoMetadata


def test_parse_playlist_input_accepts_url_and_id():
    assert (
        parse_playlist_input("https://www.youtube.com/playlist?list=PLtest12345")
        == "PLtest12345"
    )
    assert parse_playlist_input("PLtest12345") == "PLtest12345"


def test_parse_playlist_input_preserves_underscore_playlist_id():
    url = "https://www.youtube.com/playlist?list=PLOkQ3xh7s_rk"
    assert parse_playlist_input(url) == "PLOkQ3xh7s_rk"
    assert parse_playlist_input("PLOkQ3xh7s_rk") == "PLOkQ3xh7s_rk"


def test_iter_playlist_video_entries_paginates():
    calls: list[str | None] = []

    def fake_get(url: str, timeout: float):
        if "playlists?" in url:
            return {"items": [{"snippet": {"title": "My Playlist"}}]}
        if "pageToken=page2" in url:
            calls.append(url)
            return {
                "items": [
                    {
                        "snippet": {"title": "Third"},
                        "contentDetails": {"videoId": "vid00000003"},
                    }
                ]
            }
        calls.append(url)
        return {
            "items": [
                {
                    "snippet": {"title": "First"},
                    "contentDetails": {"videoId": "vid00000001"},
                },
                {
                    "snippet": {"title": "Second"},
                    "contentDetails": {"videoId": "vid00000002"},
                },
            ],
            "nextPageToken": "page2",
        }

    client = YouTubeDataApiClient("key", http_get=fake_get)
    refs = list(client.iter_playlist_video_entries("PLtest12345"))
    assert [video_id for video_id, _, _ in refs] == [
        "vid00000001",
        "vid00000002",
        "vid00000003",
    ]
    assert len(calls) == 2


def test_list_playlist_videos_deduplicates_by_video_id():
    client = MagicMock(spec=YouTubeDataApiClient)
    client.get_playlist_title.return_value = "Course"
    client.iter_playlist_video_entries.return_value = iter(
        [
            ("abc12345678", "A", None),
            ("abc12345678", "A duplicate row", None),
            ("def98765432", "B", None),
        ]
    )
    title, refs = list_playlist_videos(client, "PLx")
    assert title == "Course"
    assert [ref.video_id for ref in refs] == ["abc12345678", "def98765432"]


def test_audit_playlist_classifies_archive_gaps():
    client = MagicMock(spec=YouTubeDataApiClient)
    client.get_playlist_title.return_value = "AI Talks"
    client.iter_playlist_video_entries.return_value = iter(
        [
            ("archived1111", "Done", None),
            ("missing2222", "Need transcript", None),
            ("gone3333333", "Deleted", None),
        ]
    )
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    pub_iso = now.isoformat()
    client.get_videos_details.return_value = {
        "archived1111": ApiVideoDetails(
            video_id="archived1111",
            channel_id="UC",
            title="Done",
            published_at_utc=now,
            privacy_status="public",
            upload_status="processed",
            live_broadcast_content="none",
            duration_seconds=60,
        ),
        "missing2222": ApiVideoDetails(
            video_id="missing2222",
            channel_id="UC",
            title="Need transcript",
            published_at_utc=now,
            privacy_status="public",
            upload_status="processed",
            live_broadcast_content="none",
            duration_seconds=60,
        ),
    }

    index = {
        "videos": {
            "archived1111": {"transcript_status": "success"},
        }
    }
    report = audit_playlist_against_archive(client, "PLaudit", index)
    assert [item.video_id for item in report.already_archived] == ["archived1111"]
    assert [item.video_id for item in report.missing_transcripts] == ["missing2222"]
    assert [item.video_id for item in report.unavailable] == ["gone3333333"]
    payload = report.to_dict()
    assert payload["missing_transcripts"][0]["youtube_published_at_utc"] == pub_iso
    assert payload["already_archived"][0]["youtube_published_at_utc"] == pub_iso


def test_parse_playlist_input_rejects_invalid():
    with pytest.raises(ValueError):
        parse_playlist_input("")


def test_list_playlist_videos_falls_back_to_ytdlp_on_api_404():
    client = MagicMock(spec=YouTubeDataApiClient)
    client.get_playlist_title.side_effect = YouTubeApiError(
        "YouTube API HTTP 404: cannot be found",
        status=404,
        reason="cannot be found",
    )
    ytdlp_meta = PlaylistMetadata(
        playlist_id="PLOkQ3xh7s_rk",
        title="Evals",
        url="https://www.youtube.com/playlist?list=PLOkQ3xh7s_rk",
        videos=[
            VideoMetadata("vid00000001", "One", "https://youtu.be/vid00000001"),
            VideoMetadata("vid00000002", "Two", "https://youtu.be/vid00000002"),
        ],
    )
    with patch(
        "discovery.playlist.fetch_playlist_metadata",
        return_value=ytdlp_meta,
    ) as fetch_mock:
        title, refs = list_playlist_videos(client, "PLOkQ3xh7s_rk")
    fetch_mock.assert_called_once_with("PLOkQ3xh7s_rk")
    assert title == "Evals"
    assert [ref.video_id for ref in refs] == ["vid00000001", "vid00000002"]
    client.iter_playlist_video_entries.assert_not_called()


def test_list_playlist_videos_falls_back_when_api_returns_no_items():
    client = MagicMock(spec=YouTubeDataApiClient)
    client.get_playlist_title.return_value = None
    client.iter_playlist_video_entries.return_value = iter([])
    video_ids = [f"vid{i:08d}" for i in range(1, 7)]
    ytdlp_meta = PlaylistMetadata(
        playlist_id="PLOkQ3xh7s_rk",
        title="Evals",
        url="https://www.youtube.com/playlist?list=PLOkQ3xh7s_rk",
        videos=[
            VideoMetadata(vid, f"Talk {i}", f"https://youtu.be/{vid}")
            for i, vid in enumerate(video_ids, start=1)
        ],
    )
    with patch(
        "discovery.playlist.fetch_playlist_metadata",
        return_value=ytdlp_meta,
    ):
        title, refs = list_playlist_videos(client, "PLOkQ3xh7s_rk")
    assert title == "Evals"
    assert len(refs) == 6
    assert [ref.position for ref in refs] == list(range(1, 7))
