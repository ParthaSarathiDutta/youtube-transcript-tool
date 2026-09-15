#!/usr/bin/env python3
"""CLI entry point for YouTube transcript extraction."""

from __future__ import annotations

import sys
from pathlib import Path

from document_writer import write_playlist_document, write_single_video_document
from transcript_service import TranscriptResult, fetch_transcript
from youtube_utils import (
    UrlType,
    VideoMetadata,
    fetch_playlist_metadata,
    fetch_video_metadata,
    parse_youtube_url,
)


def _process_single_video(video_id: str, output_dir: Path) -> int:
    print(f"Fetching metadata for video {video_id}...")
    try:
        metadata = fetch_video_metadata(video_id)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Retrieving transcript: {metadata.title}")
    transcript = fetch_transcript(video_id)
    if not transcript.available:
        print("Warning: Transcript unavailable; document will note that.")

    output_path = write_single_video_document(metadata, transcript, output_dir=output_dir)
    print(f"Saved: {output_path.resolve()}")
    return 0


def _process_playlist(playlist_id: str, output_dir: Path) -> int:
    print(f"Fetching playlist {playlist_id}...")
    try:
        playlist = fetch_playlist_metadata(playlist_id)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1

    total = len(playlist.videos)
    if total == 0:
        print("Error: No videos found in playlist (or playlist is unavailable).")
        return 1

    print(f"Found {total} video(s) in playlist: {playlist.title}")

    items: list[tuple[VideoMetadata, TranscriptResult]] = []
    for index, video in enumerate(playlist.videos, start=1):
        print(f"[{index}/{total}] Processing: {video.title}")
        try:
            transcript = fetch_transcript(video.video_id)
        except Exception as exc:
            print(f"  Warning: Failed to fetch transcript ({exc}); continuing.")
            transcript = TranscriptResult.unavailable()
        if not transcript.available:
            print("  Warning: Transcript unavailable.")
        items.append((video, transcript))

    output_path = write_playlist_document(playlist, items, output_dir=output_dir)
    print(f"Saved: {output_path.resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print('Usage: python main.py "<youtube_url>"')
        return 1

    url = args[0].strip()
    url_type, resource_id = parse_youtube_url(url)

    if url_type == UrlType.INVALID or not resource_id:
        print("Error: Invalid or unsupported YouTube URL.")
        return 1

    output_dir = Path.cwd()

    if url_type == UrlType.PLAYLIST:
        return _process_playlist(resource_id, output_dir)

    return _process_single_video(resource_id, output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
