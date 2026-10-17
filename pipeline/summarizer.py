"""OpenAI summarization grounded in transcript text."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

SUMMARY_PROMPT = """You are summarizing a YouTube transcript for an AI engineering learner.

Grounding rules (strict):
- Every factual claim must appear in the transcript or in the verified video metadata block.
- Do not invent tools, examples, timestamps, or case studies.
- Auto-generated captions mishear names and model names; treat transcript wording as fallible.
- Use verified metadata for speaker and organization spelling when the transcript disagrees; note likely ASR errors briefly.
- Never invent or guess acronym expansions. If the transcript uses an acronym without defining it, use the acronym only and describe what the speaker said it does—not what letters stand for.
- When the speaker is uncertain ("I think", "I believe", "not sure"), preserve that uncertainty in the summary.
- Flag model names and dollar/token/numeric claims that may be ASR errors—mark them (verify against slides/recording) or quote transcript wording.
- Do not add generic interview-prep fluff; each bullet must reflect something specific from this talk.
- Prefer concrete frameworks, tradeoffs, and numbers the speaker actually gave over vague advice.
- If the transcript lacks detail for a section, write "Not stated in transcript." and keep the section short.

Return Markdown with exactly these sections:
## Main concept
## Technical concepts
## Tools, libraries, architectures
## Engineering insights and tradeoffs
## Examples mentioned (transcript only)
## Uncertain names, numbers, and acronyms
## AI engineering interview takeaways
## Practical next steps
## Timestamps (only if clearly present in transcript)
"""


@dataclass(frozen=True)
class VideoTitleMetadata:
    title: str
    speaker: str | None = None
    organization: str | None = None


def parse_video_title_metadata(title: str) -> VideoTitleMetadata:
    """Parse speaker and organization from common YouTube title suffixes."""
    cleaned = title.strip()
    speaker: str | None = None
    organization: str | None = None
    suffix = cleaned
    for sep in (" — ", " – ", " - "):
        if sep in cleaned:
            suffix = cleaned.rsplit(sep, 1)[-1].strip()
            break
    if "," in suffix:
        left, right = (part.strip() for part in suffix.split(",", 1))
        if left and right and len(left.split()) <= 5:
            speaker, organization = left, right
    return VideoTitleMetadata(title=cleaned, speaker=speaker, organization=organization)


def _metadata_block(meta: VideoTitleMetadata) -> str:
    lines = ["Verified video metadata (authoritative for speaker/company spelling):"]
    lines.append(f"- Title: {meta.title}")
    if meta.speaker:
        lines.append(f"- Speaker: {meta.speaker}")
    if meta.organization:
        lines.append(f"- Organization: {meta.organization}")
    if meta.speaker or meta.organization:
        lines.append(
            "- If the transcript spells names differently, prefer this metadata and "
            "treat transcript variants as possible caption errors."
        )
    return "\n".join(lines)


def _build_user_content(
    *,
    title: str,
    youtube_url: str,
    transcript_text: str,
    chunk_index: int | None = None,
    chunk_total: int | None = None,
) -> str:
    meta = parse_video_title_metadata(title)
    parts = [_metadata_block(meta), f"URL: {youtube_url}"]
    if chunk_index is not None and chunk_total is not None:
        parts.append(f"Transcript part {chunk_index}/{chunk_total}:")
    else:
        parts.append("Transcript:")
    parts.append(transcript_text)
    return "\n\n".join(parts)


@dataclass(frozen=True)
class SummaryResult:
    markdown: str
    model: str
    mocked: bool = False


def _chunk_text(text: str, max_chars: int = 12000) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + max_chars])
        start += max_chars
    return chunks


def summarize_transcript(
    *,
    title: str,
    youtube_url: str,
    transcript_text: str,
    api_key: str | None,
    model: str,
    mock: bool = False,
) -> SummaryResult:
    if mock or not api_key:
        body = (
            f"## Main concept\nMock summary for **{title}** ({youtube_url}).\n\n"
            "## Technical concepts\nNot generated (OPENAI_API_KEY missing or --mock-llm).\n"
        )
        return SummaryResult(markdown=body, model=model, mocked=True)

    chunks = _chunk_text(transcript_text)
    partials: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        user_content = _build_user_content(
            title=title,
            youtube_url=youtube_url,
            transcript_text=chunk,
            chunk_index=index,
            chunk_total=len(chunks),
        )
        partials.append(_call_openai(api_key, model, user_content))

    if len(partials) == 1:
        return SummaryResult(markdown=partials[0], model=model)

    merge_prompt = (
        "Merge these partial summaries into one cohesive Markdown summary "
        "without repeating sections. Apply the same grounding rules from the system prompt. "
        "Keep the ## Uncertain names, numbers, and acronyms section consolidated.\n\n"
        + "\n\n---\n\n".join(partials)
    )
    merged = _call_openai(api_key, model, merge_prompt)
    return SummaryResult(markdown=merged, model=model)


def _call_openai(api_key: str, model: str, user_content: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error {exc.code}: {body[:500]}") from exc
    except TimeoutError as exc:
        raise RuntimeError("OpenAI API timeout") from exc

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI API returned no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise RuntimeError("OpenAI API returned empty content")
    return str(content).strip()
