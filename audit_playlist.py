#!/usr/bin/env python3
"""Audit a YouTube playlist against the local/GitHub-synced transcript archive."""

from __future__ import annotations

import argparse
import json
import sys

from discovery.playlist import audit_playlist_against_archive, parse_playlist_input
from discovery.youtube_data_api import YouTubeApiError, YouTubeDataApiClient
from pipeline.archive_store import load_index, sync_archive_from_github
from pipeline.config import load_config
from pipeline.git_archive import ArchiveGitError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List playlist videos and compare to archive index (read-only).",
    )
    parser.add_argument(
        "playlist",
        help="YouTube playlist URL or playlist ID.",
    )
    args = parser.parse_args(argv)

    try:
        playlist_id = parse_playlist_input(args.playlist)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2

    config = load_config()
    if not config.youtube_api_key:
        print(json.dumps({"error": "YOUTUBE_API_KEY is required."}), file=sys.stderr)
        return 2

    try:
        sync_archive_from_github(
            archive_root=config.archive_root,
            repo=config.archive_github_repo,
            token=config.archive_github_token,
        )
    except ArchiveGitError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    index = load_index(config.archive_root)
    client = YouTubeDataApiClient(config.youtube_api_key)

    try:
        report = audit_playlist_against_archive(client, playlist_id, index)
    except YouTubeApiError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps(report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
