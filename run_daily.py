#!/usr/bin/env python3
"""Run the end-to-end daily digest pipeline."""

from __future__ import annotations

import argparse
import json
import sys

from pipeline.config import load_config
from pipeline.daily_run import run_daily_digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Engineer daily digest pipeline.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write archive + email previews; do not send email or push GitHub.",
    )
    parser.add_argument(
        "--send-email",
        action="store_true",
        help="Send email when SMTP is configured (implies not dry-run for email).",
    )
    parser.add_argument("--mock-llm", action="store_true", help="Skip OpenAI; write mock summaries.")
    parser.add_argument("--force", action="store_true", help="Reprocess videos and resend digest.")
    parser.add_argument(
        "--video-id",
        help="Vertical slice: process one video (bypasses channel discovery list).",
    )
    parser.add_argument(
        "--video-ids",
        help="Comma-separated video IDs for a multi-video digest test/run.",
    )
    args = parser.parse_args(argv)

    dry_run = args.dry_run or not args.send_email
    config = load_config()

    video_ids = None
    if args.video_ids:
        video_ids = [part.strip() for part in args.video_ids.split(",") if part.strip()]
    video_id = args.video_id if not video_ids else None

    try:
        result = run_daily_digest(
            config,
            dry_run=dry_run,
            mock_llm=args.mock_llm,
            force=args.force,
            video_id=video_id,
            video_ids=video_ids,
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps(result.__dict__, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
