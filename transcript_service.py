"""YouTube transcript retrieval and text formatting."""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html import unescape
from pathlib import Path

import yt_dlp
from youtube_transcript_api import (
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)
from youtube_transcript_api._errors import CouldNotRetrieveTranscript

_SUBTITLE_FORMAT_PRIORITY = ("json3", "vtt", "srv1", "ttml")
_VTT_TIMESTAMP = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})"
)
_HTML_TAG = re.compile(r"<[^>]+>")
_TRANSCRIPTAPI_BASE_URL = "https://transcriptapi.com/api/v2/youtube/transcript"


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    duration: float
    text: str


@dataclass
class TranscriptResult:
    segments: list[TranscriptSegment]
    language_code: str
    is_generated: bool
    display_text: str
    available: bool = True
    error_message: str | None = None
    provider: str | None = None

    @classmethod
    def unavailable(cls, message: str = "Transcript unavailable.") -> TranscriptResult:
        return cls(
            segments=[],
            language_code="",
            is_generated=False,
            display_text=message,
            available=False,
            error_message=message,
        )


def _format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def segments_to_paragraphs(segments: list[TranscriptSegment], pause_threshold: float = 2.0) -> str:
    """Combine caption segments into readable paragraphs."""
    if not segments:
        return ""

    paragraphs: list[str] = []
    current_parts: list[str] = []
    last_end: float | None = None

    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue

        if (
            last_end is not None
            and segment.start - last_end >= pause_threshold
            and current_parts
        ):
            paragraphs.append(" ".join(current_parts))
            current_parts = []

        current_parts.append(text)
        last_end = segment.start + segment.duration

    if current_parts:
        paragraphs.append(" ".join(current_parts))

    return "\n\n".join(paragraphs)


def _clean_caption_text(text: str) -> str:
    text = unescape(text)
    text = _HTML_TAG.sub("", text)
    text = text.replace("\n", " ").strip()
    return re.sub(r"\s+", " ", text)


def _dedupe_consecutive_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    deduped: list[TranscriptSegment] = []
    previous_text = ""
    for segment in segments:
        normalized = segment.text.strip()
        if not normalized or normalized == previous_text:
            continue
        deduped.append(segment)
        previous_text = normalized
    return deduped


def _vtt_time_to_seconds(timestamp: str) -> float:
    parts = timestamp.split(":")
    if len(parts) == 3:
        hours, minutes, rest = parts
        seconds, millis = rest.split(".")
        return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0
    minutes, rest = parts
    seconds, millis = rest.split(".")
    return int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


def _parse_vtt(content: str) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or lines[0].upper() == "WEBVTT":
            continue
        if "-->" not in lines[0] and len(lines) > 1 and "-->" in lines[1]:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        match = _VTT_TIMESTAMP.search(lines[0])
        if not match:
            continue
        start = _vtt_time_to_seconds(match.group("start"))
        end = _vtt_time_to_seconds(match.group("end"))
        text = _clean_caption_text(" ".join(lines[1:]))
        if text:
            segments.append(TranscriptSegment(start=start, duration=max(end - start, 0.0), text=text))
    return _dedupe_consecutive_segments(segments)


def _parse_json3(content: str) -> list[TranscriptSegment]:
    data = json.loads(content)
    segments: list[TranscriptSegment] = []
    for event in data.get("events") or []:
        if not event.get("segs"):
            continue
        start = float(event.get("tStartMs", 0)) / 1000.0
        duration = float(event.get("dDurationMs", 0)) / 1000.0
        text = _clean_caption_text("".join(part.get("utf8", "") for part in event["segs"]))
        if text:
            segments.append(TranscriptSegment(start=start, duration=duration, text=text))
    return _dedupe_consecutive_segments(segments)


def _parse_subtitle_content(content: str, ext: str) -> list[TranscriptSegment]:
    ext = ext.lower()
    if ext == "json3":
        return _parse_json3(content)
    if ext in ("vtt", "srv1", "ttml"):
        return _parse_vtt(content)
    return []


def _pick_subtitle_format(formats: list[dict]) -> dict | None:
    for ext in _SUBTITLE_FORMAT_PRIORITY:
        for item in formats:
            if item.get("ext") == ext and item.get("url"):
                return item
    for item in formats:
        if item.get("url"):
            return item
    return None


def _result_from_segments(
    segments: list[TranscriptSegment],
    language_code: str,
    is_generated: bool,
    *,
    provider: str | None = None,
) -> TranscriptResult | None:
    display_text = segments_to_paragraphs(segments)
    if not display_text.strip():
        return None
    return TranscriptResult(
        segments=segments,
        language_code=language_code,
        is_generated=is_generated,
        display_text=display_text,
        available=True,
        provider=provider,
    )


def _ytdlp_subtitle_opts(use_browser_cookies: bool) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    if use_browser_cookies:
        opts["cookiesfrombrowser"] = ("chrome",)
    return opts


def _browser_cookie_subtitles_enabled() -> bool:
    """GitHub-hosted runners have no Chrome profile; skip cookiesfrombrowser there."""
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        return False
    if os.environ.get("TRANSCRIPT_SKIP_BROWSER_COOKIES", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    return True


def _fetch_via_ytdlp_subtitles(
    video_id: str,
    preferred_languages: list[str],
) -> TranscriptResult | None:
    """Download English subtitles with yt-dlp (no video/audio download)."""
    cookie_attempts = (True, False) if _browser_cookie_subtitles_enabled() else (False,)
    for use_browser_cookies in cookie_attempts:
        result = _fetch_via_ytdlp_subtitles_once(
            video_id, preferred_languages, use_browser_cookies=use_browser_cookies
        )
        if result:
            return _apply_provider(result, "yt-dlp")
    return None


def _try_subtitles_from_info(
    ydl: yt_dlp.YoutubeDL,
    info: dict,
    preferred_languages: list[str],
) -> TranscriptResult | None:
    manual_subs = info.get("subtitles") or {}
    auto_subs = info.get("automatic_captions") or {}

    for is_generated, catalog in ((False, manual_subs), (True, auto_subs)):
        for lang in preferred_languages:
            formats = catalog.get(lang)
            if not formats:
                continue
            subtitle_format = _pick_subtitle_format(formats)
            if not subtitle_format:
                continue
            ext = subtitle_format.get("ext") or "vtt"
            try:
                raw = ydl.urlopen(subtitle_format["url"]).read().decode("utf-8", errors="replace")
            except Exception:
                continue
            segments = _parse_subtitle_content(raw, ext)
            result = _result_from_segments(segments, lang, is_generated)
            if result:
                return result
    return None


def _try_subtitle_files_with_ydl(
    ydl: yt_dlp.YoutubeDL,
    watch_url: str,
    preferred_languages: list[str],
) -> TranscriptResult | None:
    """Write subtitle files using an existing YoutubeDL instance (same cookie session)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        outtmpl = str(Path(tmp_dir) / "%(id)s")
        ydl.params.update(
            {
                "subtitlesformat": "vtt/json3/best",
                "subtitleslangs": preferred_languages,
                "outtmpl": outtmpl,
            }
        )
        for is_generated, flag in ((False, "writesubtitles"), (True, "writeautomaticsub")):
            ydl.params["writesubtitles"] = flag == "writesubtitles"
            ydl.params["writeautomaticsub"] = flag == "writeautomaticsub"
            try:
                ydl.download([watch_url])
            except Exception:
                continue

            for lang in preferred_languages:
                for ext in ("json3", "vtt"):
                    for path in Path(tmp_dir).glob(f"*.{lang}.{ext}"):
                        try:
                            raw = path.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            continue
                        segments = _parse_subtitle_content(raw, ext)
                        result = _result_from_segments(segments, lang, is_generated)
                        if result:
                            return result
                for path in Path(tmp_dir).glob("*.vtt"):
                    try:
                        raw = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    segments = _parse_subtitle_content(raw, "vtt")
                    result = _result_from_segments(segments, "en", is_generated)
                    if result:
                        return result
    return None


def _fetch_via_ytdlp_subtitles_once(
    video_id: str,
    preferred_languages: list[str],
    *,
    use_browser_cookies: bool,
) -> TranscriptResult | None:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = _ytdlp_subtitle_opts(use_browser_cookies)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = None
        try:
            info = ydl.extract_info(watch_url, download=False)
        except Exception:
            info = None

        if info:
            result = _try_subtitles_from_info(ydl, info, preferred_languages)
            if result:
                return result

        return _try_subtitle_files_with_ydl(ydl, watch_url, preferred_languages)


def _load_transcript_api_key() -> str | None:
    try:
        from dotenv import load_dotenv

        env_path = Path(__file__).resolve().parent / ".env"
        load_dotenv(env_path)
    except ImportError:
        pass

    api_key = os.environ.get("TRANSCRIPT_API_KEY", "").strip()
    return api_key or None


def _transcriptapi_language_param(preferred_languages: list[str]) -> str:
    """Build TranscriptAPI language priority list (manual English before auto-generated)."""
    codes: list[str] = []
    for lang in preferred_languages:
        if lang not in codes:
            codes.append(lang)
    if "en" not in codes:
        codes.append("en")
    if "asr-en" not in codes:
        codes.append("asr-en")
    return ",".join(codes[:10])


def _fetch_via_transcriptapi(
    video_id: str,
    preferred_languages: list[str],
) -> TranscriptResult | None:
    api_key = _load_transcript_api_key()
    if not api_key:
        return None

    query = urllib.parse.urlencode(
        {
            "video_url": video_id,
            "format": "json",
            "include_timestamp": "true",
            "language": _transcriptapi_language_param(preferred_languages),
        }
    )
    request = urllib.request.Request(
        f"{_TRANSCRIPTAPI_BASE_URL}?{query}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "YoutubeTranscriptTool/1.0 (TranscriptAPI client)",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError):
        return None
    except Exception:
        return None

    transcript_items = payload.get("transcript")
    if not isinstance(transcript_items, list) or not transcript_items:
        return None

    segments: list[TranscriptSegment] = []
    for item in transcript_items:
        if not isinstance(item, dict):
            continue
        text = _clean_caption_text(str(item.get("text", "")))
        if not text:
            continue
        start = float(item.get("start", 0.0))
        duration = float(item.get("duration", 0.0))
        segments.append(TranscriptSegment(start=start, duration=duration, text=text))

    segments = _dedupe_consecutive_segments(segments)
    language_code = str(payload.get("language") or "en")
    is_generated = language_code.lower().startswith("asr")
    return _result_from_segments(segments, language_code, is_generated)


def _fetch_via_transcript_api(
    video_id: str,
    preferred_languages: list[str],
) -> TranscriptResult:
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)
    transcript = transcript_list.find_transcript(preferred_languages)
    fetched = transcript.fetch()
    segments = [
        TranscriptSegment(start=s.start, duration=s.duration, text=s.text)
        for s in fetched.snippets
    ]
    result = _result_from_segments(
        segments,
        fetched.language_code,
        fetched.is_generated,
        provider="youtube_transcript_api",
    )
    if not result:
        return TranscriptResult.unavailable()
    return result


def _apply_provider(result: TranscriptResult | None, provider: str) -> TranscriptResult | None:
    if result is None or not result.available:
        return result
    result.provider = provider
    return result


def fetch_transcript(video_id: str, preferred_languages: list[str] | None = None) -> TranscriptResult:
    """
    Fetch English transcript when possible.
    Prefers manually created captions over auto-generated (library default).
    Falls back to yt-dlp subtitles, then TranscriptAPI, when earlier methods fail.
    """
    languages = preferred_languages or ["en", "en-US", "en-GB"]
    failures: list[str] = []

    try:
        return _fetch_via_transcript_api(video_id, languages)
    except (IpBlocked, RequestBlocked) as exc:
        failures.append(f"youtube_transcript_api:{exc.__class__.__name__}")
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable, CouldNotRetrieveTranscript) as exc:
        failures.append(f"youtube_transcript_api:{exc.__class__.__name__}")
    except Exception as exc:
        failures.append(f"youtube_transcript_api:{exc.__class__.__name__}")

    fallback = _apply_provider(_fetch_via_ytdlp_subtitles(video_id, languages), "yt-dlp")
    if fallback:
        return fallback
    failures.append("yt-dlp:no_result")

    transcriptapi = _apply_provider(_fetch_via_transcriptapi(video_id, languages), "transcriptapi")
    if transcriptapi:
        return transcriptapi
    failures.append("transcriptapi:no_result")

    message = "Transcript unavailable. Attempts: " + "; ".join(failures)
    return TranscriptResult.unavailable(message)


def segments_with_timestamps(segments: list[TranscriptSegment]) -> str:
    """Optional debug/export helper preserving timestamps per line."""
    lines = [
        f"[{_format_timestamp(seg.start)}] {seg.text.strip()}"
        for seg in segments
        if seg.text.strip()
    ]
    return "\n".join(lines)
