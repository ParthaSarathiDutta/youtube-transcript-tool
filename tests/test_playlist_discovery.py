"""Playlist discovery pagination and archive deduplication."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from discovery.playlist import (
    audit_playlist_against_archive,
    list_playlist_videos,
    parse_playlist_input,
)
from discovery.youtube_data_api import ApiVideoDetails, YouTubeDataApiClient
from datetime import datetime, timezone


def test_parse_playlist_input_accepts_url_and_id():
    assert (
        parse_playlist_input("https://www.youtube.com/playlist?list=PLtest12345")
        == "PLtest12345"
    )
    assert parse_playlist_input("PLtest12345") == "PLtest12345"


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
    assert [video_id for video_id, _ in refs] == [
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
            ("abc12345678", "A"),
            ("abc12345678", "A duplicate row"),
            ("def98765432", "B"),
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
            ("archived1111", "Done"),
            ("missing2222", "Need transcript"),
            ("gone3333333", "Deleted"),
        ]
    )
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
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


def test_parse_playlist_input_rejects_invalid():
    with pytest.raises(ValueError):
        parse_playlist_input("")
