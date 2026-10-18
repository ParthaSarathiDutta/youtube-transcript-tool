#!/usr/bin/env python3
"""Sync the Evals YouTube playlist into archive/responsiv/evals/."""

from __future__ import annotations

import argparse
import json
import sys

from pipeline.config import load_config
from pipeline.evals_playlist_sync import EVALS_PLAYLIST_ID, sync_evals_playlist


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync Evals playlist transcripts into responsiv/evals (Phase 2).",
    )
    parser.add_argument(
        "--playlist",
        default=f"https://www.youtube.com/playlist?list={EVALS_PLAYLIST_ID}",
        help="Playlist URL or ID (default: Evals playlist).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only; do not write transcripts or push archive.",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push archive to GitHub after a successful sync (never with --dry-run).",
    )
    parser.add_argument(
        "--no-pull",
        action="store_true",
        help="Skip pulling the remote archive before sync.",
    )
    parser.add_argument(
        "--video-id",
        help="Process a single playlist video (smoke test / incremental sync).",
    )
    args = parser.parse_args(argv)

    if args.push and args.dry_run:
        print(json.dumps({"error": "Cannot --push with --dry-run."}), file=sys.stderr)
        return 2

    config = load_config()
    try:
        result = sync_evals_playlist(
            config,
            playlist=args.playlist,
            dry_run=args.dry_run,
            push=args.push,
            pull=not args.no_pull,
            video_id=args.video_id,
        )
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
