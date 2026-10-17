"""Environment configuration for the daily digest pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class PipelineConfig:
    youtube_api_key: str | None
    youtube_channel_handle: str
    openai_api_key: str | None
    openai_model: str
    archive_root: Path
    archive_github_repo: str | None
    archive_github_token: str | None
    email_smtp_host: str | None
    email_smtp_port: int
    email_smtp_user: str | None
    email_smtp_password: str | None
    email_from: str | None
    email_to: str | None
    discovery_lookback_hours: float
    discovery_recovery_days: int
    send_email_on_zero_videos: bool


def load_config() -> PipelineConfig:
    load_dotenv()
    return PipelineConfig(
        youtube_api_key=os.environ.get("YOUTUBE_API_KEY", "").strip() or None,
        youtube_channel_handle=os.environ.get("YOUTUBE_CHANNEL_HANDLE", "aiDotEngineer"),
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip() or None,
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        archive_root=Path(os.environ.get("ARCHIVE_ROOT", "archive")).resolve(),
        archive_github_repo=os.environ.get("ARCHIVE_GITHUB_REPO", "").strip() or None,
        archive_github_token=os.environ.get("ARCHIVE_GITHUB_TOKEN", "").strip() or None,
        email_smtp_host=os.environ.get("EMAIL_SMTP_HOST", "").strip() or None,
        email_smtp_port=int(os.environ.get("EMAIL_SMTP_PORT", "587")),
        email_smtp_user=os.environ.get("EMAIL_SMTP_USER", "").strip() or None,
        email_smtp_password=os.environ.get("EMAIL_SMTP_PASSWORD", "").strip() or None,
        email_from=os.environ.get("EMAIL_FROM", "").strip() or None,
        email_to=os.environ.get("EMAIL_TO", "").strip() or None,
        discovery_lookback_hours=float(os.environ.get("DISCOVERY_LOOKBACK_HOURS", "24")),
        discovery_recovery_days=int(os.environ.get("DISCOVERY_RECOVERY_LOOKBACK_DAYS", "7")),
        send_email_on_zero_videos=os.environ.get("SEND_EMAIL_ON_ZERO_VIDEOS", "false").lower()
        == "true",
    )
