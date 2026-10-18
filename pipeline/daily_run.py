"""End-to-end daily digest pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from discovery.models import DiscoveryCategory, DiscoveryRecord, DiscoveryStatus, VideoType
from discovery.service import VideoDiscoveryService
from discovery.window import CHICAGO, REPORTING_WINDOW_POLICY
from discovery.youtube_data_api import YouTubeDataApiClient
from document_writer import DailyVideoDocument
from pipeline.archive_index import upsert_video_index_entry
from pipeline.archive_paths import migrate_archive_video_folders
from pipeline.archive_store import (
    _email_section_text,
    digest_day_paths,
    load_hydrated_video,
    load_index,
    mark_videos_digest_emailed,
    maybe_push_to_github_archive,
    save_index,
    sync_archive_from_github,
    video_already_processed,
    video_digest_email_sent,
    write_daily_bundle,
    write_video_artifacts,
)
from pipeline.git_archive import ArchiveGitError
from pipeline.config import PipelineConfig
from pipeline.email_delivery import send_digest_email
from pipeline.summarizer import SummaryResult, summarize_transcript
from transcript_service import TranscriptResult, fetch_transcript

@dataclass
class PipelineRunResult:
    digest_date: date
    archive_dir: str
    videos_processed: int
    videos_failed: list[str]
    email_status: str
    email_detail: str
    llm_mocked: bool
    archive_push: str | None
    discovery_count: int
    archive_pull: str | None = None


def run_daily_digest(
    config: PipelineConfig,
    *,
    digest_date: date | None = None,
    dry_run: bool = True,
    mock_llm: bool = False,
    force: bool = False,
    video_id: str | None = None,
    video_ids: list[str] | None = None,
) -> PipelineRunResult:
    digest_date = digest_date or datetime.now(CHICAGO).date()
    archive_pull: str | None = None
    try:
        archive_pull = sync_archive_from_github(
            archive_root=config.archive_root,
            repo=config.archive_github_repo,
            token=config.archive_github_token,
        )
    except ArchiveGitError as exc:
        raise RuntimeError(str(exc)) from exc

    paths = digest_day_paths(config.archive_root, digest_date)
    paths["day_dir"].mkdir(parents=True, exist_ok=True)
    paths["videos_dir"].mkdir(parents=True, exist_ok=True)
    index = load_index(config.archive_root)
    migrate_archive_video_folders(config.archive_root, index)
    save_index(config.archive_root, index)

    manifest: dict[str, Any] = {
        "digest_date": digest_date.isoformat(),
        "reporting_window_policy": REPORTING_WINDOW_POLICY,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "videos": [],
        "email_status": None,
    }
    if paths["manifest"].exists() and not force:
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))

    records = _discover(config, video_id=video_id, video_ids=video_ids)
    records = _records_for_daily_digest(
        records, explicit=bool(video_id or video_ids)
    )
    email_doc_items: list[DailyVideoDocument] = []
    email_sections: list[str] = []
    new_transcripts_count = 0
    videos_failed: list[str] = []
    summaries_enabled = config.enable_summaries
    manifest["summaries_enabled"] = summaries_enabled
    llm_mocked = (
        not summaries_enabled
        or mock_llm
        or not config.openai_api_key
    )

    hydrated_ids: set[str] = set()

    for record in records:
        if not force and video_already_processed(index, record.video_id):
            include_in_email = not video_digest_email_sent(index, record.video_id)
            _hydrate_video_artifacts(
                paths["videos_dir"],
                record.video_id,
                email_doc_items,
                email_sections,
                manifest,
                hydrated_ids,
                include_in_email=include_in_email,
                index=index,
            )
            continue
        if record.status.value not in ("discovered",):
            videos_failed.append(f"{record.video_id}:{record.status.value}")
            continue

        transcript = fetch_transcript(record.video_id)
        if not transcript.available:
            videos_failed.append(f"{record.video_id}:transcript")
            _update_index(index, record, transcript, success=False)
            continue

        summary: SummaryResult | None = None
        summary_md: str | None = None
        if summaries_enabled:
            summary = summarize_transcript(
                title=record.title,
                youtube_url=record.youtube_url,
                transcript_text=transcript.display_text,
                api_key=config.openai_api_key,
                model=config.openai_model,
                mock=mock_llm or not config.openai_api_key,
            )
            summary_md = summary.markdown

        metadata = {
            "video_id": record.video_id,
            "title": record.title,
            "youtube_url": record.youtube_url,
            "published_at_utc": record.published_at_utc.isoformat(),
            "discovery_category": record.discovery_category.value,
            "transcript_status": "success",
            "transcript_provider": transcript.provider,
            "caption_language": transcript.language_code,
            "captions_generated": transcript.is_generated,
            "summary_enabled": summaries_enabled,
        }
        if summary is not None:
            metadata["summary_model"] = summary.model
            metadata["summary_mocked"] = summary.mocked
        transcript_md = (
            f"# {record.title}\n\n"
            f"URL: {record.youtube_url}\n\n"
            f"{transcript.display_text}\n"
        )
        entry = write_video_artifacts(
            videos_dir=paths["videos_dir"],
            video_id=record.video_id,
            metadata=metadata,
            transcript_md=transcript_md,
            summary_md=summary_md,
        )
        manifest["videos"].append(entry.__dict__)
        _update_index(index, record, transcript, success=True)

        email_doc_items.append(
            DailyVideoDocument(
                video_id=record.video_id,
                title=record.title,
                url=record.youtube_url,
                published_at_utc=record.published_at_utc,
                transcript=transcript,
            )
        )
        email_sections.append(
            _format_email_video_section(
                record,
                transcript.provider,
                summary.markdown if summary else None,
                summaries_enabled=summaries_enabled,
            )
        )
        hydrated_ids.add(record.video_id)
        new_transcripts_count += 1

    digest_title = f"AI Engineer Daily Transcripts — {digest_date.isoformat()}"
    daily_summary_md = _build_daily_summary_md(
        digest_date,
        records,
        email_sections,
        videos_failed,
        summaries_enabled=summaries_enabled,
    )
    write_daily_bundle(
        archive_root=config.archive_root,
        digest_date=digest_date,
        digest_title=digest_title,
        daily_summary_md=daily_summary_md,
        doc_items=email_doc_items,
        manifest=manifest,
    )
    save_index(config.archive_root, index)

    archive_push = _push_archive_if_configured(config, dry_run=dry_run)

    email_status = "skipped"
    email_detail = "No new videos for digest email."
    has_attachment = paths["combined_docx"].exists() and bool(email_doc_items)
    should_email = bool(email_doc_items) or config.send_email_on_zero_videos
    if should_email:
        summary_sections = list(email_sections)
        if not summary_sections and paths["daily_summary"].exists():
            summary_sections = [paths["daily_summary"].read_text(encoding="utf-8")]
        subject = f"AI Engineer Daily Digest — {digest_date.strftime('%B %d, %Y')}"
        plain, html = _build_email(config, digest_date, summary_sections, videos_failed)
        email_result = send_digest_email(
            smtp_host=config.email_smtp_host or "",
            smtp_port=config.email_smtp_port,
            smtp_user=config.email_smtp_user or "",
            smtp_password=config.email_smtp_password or "",
            email_from=config.email_from or "",
            email_to=config.email_to or "",
            subject=subject,
            plain_body=plain,
            html_body=html,
            attachment_path=paths["combined_docx"],
            dry_run=dry_run or not _email_configured(config),
            preview_dir=paths["day_dir"],
        )
        email_status = email_result.status
        email_detail = email_result.detail
        if email_status == "sent":
            mark_videos_digest_emailed(
                index, [item.video_id for item in email_doc_items]
            )
            save_index(config.archive_root, index)
            manifest["email_status"] = "sent"
            manifest["email_sent_at_utc"] = datetime.now(timezone.utc).isoformat()
            manifest["last_emailed_video_ids"] = [
                item.video_id for item in email_doc_items
            ]
        else:
            manifest["email_status"] = email_status
        manifest["email_detail"] = email_detail
        paths["manifest"].write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        if email_status == "sent" and not dry_run:
            try:
                archive_push = _push_archive_if_configured(config, dry_run=False)
                manifest["archive_push_after_email"] = archive_push
                paths["manifest"].write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            except ArchiveGitError as exc:
                manifest["email_status"] = "sent_archive_push_failed"
                manifest["email_detail"] = (
                    "Email was sent but private archive push failed. "
                    "Do not resend email; fix archive push then merge manifest. "
                    f"Detail: {exc}"
                )
                paths["manifest"].write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                try:
                    _push_archive_if_configured(config, dry_run=False)
                except ArchiveGitError:
                    pass
                raise RuntimeError(manifest["email_detail"]) from exc

    return PipelineRunResult(
        digest_date=digest_date,
        archive_dir=str(paths["day_dir"]),
        videos_processed=len(email_doc_items) or new_transcripts_count,
        videos_failed=videos_failed,
        email_status=email_status,
        email_detail=email_detail,
        llm_mocked=llm_mocked,
        archive_push=archive_push,
        archive_pull=archive_pull,
        discovery_count=len(records),
    )


def _push_archive_if_configured(config: PipelineConfig, *, dry_run: bool) -> str | None:
    if dry_run:
        return None
    return maybe_push_to_github_archive(
        archive_root=config.archive_root,
        repo=config.archive_github_repo,
        token=config.archive_github_token,
    )


def _hydrate_video_artifacts(
    videos_dir: Path,
    video_id: str,
    doc_items: list[DailyVideoDocument],
    summary_sections: list[str],
    manifest: dict[str, Any],
    hydrated_ids: set[str],
    *,
    include_in_email: bool,
    index: dict[str, Any],
) -> None:
    if video_id in hydrated_ids:
        return
    folder_hint = (index.get("videos") or {}).get(video_id, {}).get("archive_folder")
    hydrated = load_hydrated_video(
        videos_dir, video_id, folder_hint=folder_hint
    )
    if not hydrated:
        return
    if include_in_email:
        doc_items.append(hydrated.doc_item)
        summary_sections.append(hydrated.email_section)
    _upsert_manifest_video(manifest, hydrated.manifest_entry)
    hydrated_ids.add(video_id)


def _upsert_manifest_video(manifest: dict[str, Any], entry: dict[str, Any]) -> None:
    videos = manifest.setdefault("videos", [])
    video_id = entry.get("video_id")
    for index, existing in enumerate(videos):
        if isinstance(existing, dict) and existing.get("video_id") == video_id:
            videos[index] = entry
            return
    videos.append(entry)


def _records_for_daily_digest(
    records: list[DiscoveryRecord],
    *,
    explicit: bool,
) -> list[DiscoveryRecord]:
    if explicit:
        return records
    allowed = {
        DiscoveryCategory.REPORTING_WINDOW,
        DiscoveryCategory.DELAYED_SINCE_ANCHOR,
    }
    return [record for record in records if record.discovery_category in allowed]


def _discover(
    config: PipelineConfig,
    *,
    video_id: str | None,
    video_ids: list[str] | None = None,
) -> list[DiscoveryRecord]:
    if video_ids:
        out: list[DiscoveryRecord] = []
        client = (
            YouTubeDataApiClient(config.youtube_api_key)
            if config.youtube_api_key
            else None
        )
        service = VideoDiscoveryService(client) if client else None
        for vid in video_ids:
            if service:
                _, record = service.discover_single_video(vid)
                if record:
                    out.append(record)
            else:
                out.append(_manual_discovery_record(vid))
        return out

    if video_id:
        if config.youtube_api_key:
            client = YouTubeDataApiClient(config.youtube_api_key)
            service = VideoDiscoveryService(client)
            _, record = service.discover_single_video(video_id)
            return [record] if record else []
        return [_manual_discovery_record(video_id)]

    if not config.youtube_api_key:
        raise ValueError("YOUTUBE_API_KEY is required for channel discovery.")
    client = YouTubeDataApiClient(config.youtube_api_key)
    service = VideoDiscoveryService(client)
    _, records = service.discover_for_handle(
        config.youtube_channel_handle,
        lookback_hours=config.discovery_lookback_hours,
        recovery_lookback_days=config.discovery_recovery_days,
    )
    return records


def _manual_discovery_record(video_id: str) -> DiscoveryRecord:
    from youtube_utils import fetch_video_metadata

    metadata = fetch_video_metadata(video_id)
    now = datetime.now(timezone.utc)
    return DiscoveryRecord(
        video_id=metadata.video_id,
        channel_id="",
        title=metadata.title,
        youtube_url=metadata.url,
        published_at_utc=now,
        discovery_timestamp_utc=now,
        video_type=VideoType.UNKNOWN,
        status=DiscoveryStatus.DISCOVERED,
        discovery_category=DiscoveryCategory.MANUAL_VIDEO_ID,
    )


def _update_index(
    index: dict[str, Any],
    record: DiscoveryRecord,
    transcript: TranscriptResult,
    *,
    success: bool,
) -> None:
    upsert_video_index_entry(index, record, transcript, success=success)


def _format_email_video_section(
    record: DiscoveryRecord,
    provider: str | None,
    summary_markdown: str | None,
    *,
    summaries_enabled: bool,
) -> str:
    return _email_section_text(
        title=record.title,
        url=record.youtube_url,
        provider=provider,
        summary_md=summary_markdown or "",
        summary_enabled=summaries_enabled,
    )


def _build_daily_summary_md(
    digest_date: date,
    records: list[DiscoveryRecord],
    summary_sections: list[str],
    failures: list[str],
    *,
    summaries_enabled: bool,
) -> str:
    title = (
        "AI Engineer Daily Summary"
        if summaries_enabled
        else "AI Engineer Daily Transcripts"
    )
    lines = [
        f"# {title} — {digest_date.isoformat()}",
        "",
        f"Discovered: {len(records)}",
        f"Processed: {len(summary_sections)}",
        f"OpenAI summaries: {'enabled' if summaries_enabled else 'disabled'}",
        "",
    ]
    if failures:
        lines.extend(["## Failures", *[f"- {item}" for item in failures], ""])
    for section in summary_sections:
        lines.append(section)
        lines.append("\n---\n")
    return "\n".join(lines)


def _build_email(
    config: PipelineConfig,
    digest_date: date,
    summary_sections: list[str],
    failures: list[str],
) -> tuple[str, str]:
    archive_note = str(config.archive_root)
    plain_parts = [
        "AI ENGINEER DAILY LEARNING DIGEST",
        "",
        f"New videos processed: {len(summary_sections)}",
        "",
    ]
    if failures:
        plain_parts.extend(["Failed videos:", *[f"- {f}" for f in failures], ""])
    plain_parts.extend(summary_sections)
    plain_parts.extend(
        [
            "",
            "DAILY LEARNING SYNTHESIS",
            "See individual sections above.",
            "",
            f"ARCHIVE: {archive_note}",
            "",
            "ATTACHMENT: transcripts-combined.docx",
        ]
    )
    plain = "\n".join(plain_parts)
    html = "<html><body><pre>" + _escape_html(plain) + "</pre></body></html>"
    return plain, html


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _email_configured(config: PipelineConfig) -> bool:
    return bool(
        config.email_smtp_host
        and config.email_smtp_user
        and config.email_smtp_password
        and config.email_from
        and config.email_to
    )
