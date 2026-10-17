#!/usr/bin/env python3
"""CLI for Phase 1: discover new @aiDotEngineer uploads via YouTube Data API v3."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from discovery.service import VideoDiscoveryService
from discovery.window import CHICAGO, build_discovery_window, digest_anchor_end_utc
from discovery.youtube_data_api import YouTubeApiError, YouTubeDataApiClient


def _parse_as_of(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            "--as-of must include a timezone offset or Z suffix."
        )
    return parsed.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover recently published videos on a YouTube channel.",
    )
    parser.add_argument(
        "--channel-handle",
        default=os.environ.get("YOUTUBE_CHANNEL_HANDLE", "aiDotEngineer"),
        help="YouTube handle without @ (default: env YOUTUBE_CHANNEL_HANDLE or aiDotEngineer).",
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=float(os.environ.get("DISCOVERY_LOOKBACK_HOURS", "24")),
        help="Primary reporting window length ending at the digest anchor (default: 24).",
    )
    parser.add_argument(
        "--recovery-lookback-days",
        type=int,
        default=int(os.environ.get("DISCOVERY_RECOVERY_LOOKBACK_DAYS", "7")),
        help="Recovery window before the reporting window (default: 7 days).",
    )
    parser.add_argument(
        "--as-of",
        type=_parse_as_of,
        default=None,
        help="ISO-8601 instant for window boundaries (default: now UTC).",
    )
    parser.add_argument(
        "--video-id",
        help="Discover a single video by ID (isolated testing).",
    )
    parser.add_argument(
        "--exclude-shorts",
        action="store_true",
        help="Skip videos classified as Shorts (duration <= 60s).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print window boundaries and results as JSON; no side effects.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        print(
            "Error: Set YOUTUBE_API_KEY in the environment or .env file.",
            file=sys.stderr,
        )
        return 1

    as_of = args.as_of or datetime.now(timezone.utc)
    window = build_discovery_window(
        as_of,
        lookback_hours=args.lookback_hours,
        recovery_lookback_days=args.recovery_lookback_days,
    )

    if args.dry_run:
        print("Dry run: no transcripts, email, or archive actions will run.", file=sys.stderr)

    client = YouTubeDataApiClient(api_key)
    service = VideoDiscoveryService(client)

    try:
        if args.video_id:
            _, record = service.discover_single_video(
                args.video_id.strip(),
                as_of=as_of,
                lookback_hours=args.lookback_hours,
                recovery_lookback_days=args.recovery_lookback_days,
                exclude_shorts=args.exclude_shorts,
            )
            records = [record] if record else []
        else:
            _, records = service.discover_for_handle(
                args.channel_handle,
                as_of=as_of,
                lookback_hours=args.lookback_hours,
                recovery_lookback_days=args.recovery_lookback_days,
                exclude_shorts=args.exclude_shorts,
            )
    except (YouTubeApiError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    anchor_local = digest_anchor_end_utc(as_of).astimezone(CHICAGO).isoformat()
    payload = {
        "dry_run": args.dry_run,
        "channel_handle": args.channel_handle,
        "digest_anchor_end_chicago": anchor_local,
        "window": {
            "as_of_utc": window.as_of_utc.isoformat(),
            "anchor_end_utc": window.anchor_end_utc.isoformat(),
            "reporting_start_utc": window.reporting_start_utc.isoformat(),
            "reporting_end_utc": window.reporting_end_utc.isoformat(),
            "recovery_start_utc": window.recovery_start_utc.isoformat(),
            "delayed_start_utc": window.delayed_start_utc.isoformat(),
            "delayed_end_utc": window.delayed_end_utc.isoformat(),
        },
        "count": len(records),
        "videos": [record.to_dict() for record in records],
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
