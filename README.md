# YouTube Transcript Tool

A Python CLI that converts individual YouTube videos or entire playlists into structured Microsoft Word transcript documents, with resilient multi-provider transcript retrieval.

## Features

- **Single-video transcript extraction** — pass any supported YouTube watch URL.
- **Full playlist processing** — one command processes every video in playlist order.
- **Playlist-order preservation** — videos appear in the same order as on YouTube.
- **One Word document per playlist** — all episodes in a single `.docx` with clear section breaks.
- **Structured output** — video title, canonical URL, and transcript body per section.
- **English caption support** — prefers manually created English captions, then auto-generated English when needed.
- **Multi-provider fallbacks** — retries through alternate transcript sources when direct YouTube access is blocked or rate-limited.
- **Graceful per-video failures** — one missing transcript does not stop the rest of a playlist.
- **Timestamp preservation** — segment timestamps are kept internally on `TranscriptResult` (not shown in Word by default).
- **Filename sanitization** — safe output filenames derived from video or playlist titles.
- **No video/audio download** — metadata and subtitles only; media files are not saved.

## Architecture

```
main.py
  → URL classification & routing (video vs playlist)
  → youtube_utils.py (metadata via yt-dlp)
  → transcript_service.py (transcript + fallbacks)
  → document_writer.py (python-docx)
  → .docx in the current working directory
```

| Module | Responsibility |
|--------|----------------|
| `main.py` | CLI entry point; routes single-video vs playlist workflows; progress logging. |
| `youtube_utils.py` | Parse/validate YouTube URLs; playlist and video metadata; filename sanitization. |
| `transcript_service.py` | Fetch transcripts, paragraph formatting, fallback chain, `TranscriptResult` / segments. |
| `document_writer.py` | Build single-video and playlist Word documents (headings, page breaks, separators). |

## Transcript retrieval strategy

The tool tries providers **in order** until a transcript is found:

1. **[youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api)** — fast, direct caption fetch when YouTube allows it.
2. **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — subtitle download only (`skip_download`); optional Chrome session cookies for blocked IPs; prefers manual English, then auto-generated.
3. **[TranscriptAPI](https://transcriptapi.com/)** — optional REST fallback when `TRANSCRIPT_API_KEY` is set (Bearer auth).

YouTube may rate-limit or block direct transcript requests depending on IP, network, or request volume. **No method is guaranteed** for every video; the chain improves reliability but cannot bypass private videos, disabled captions, or provider outages.

## Installation

```bash
git clone https://github.com/ParthaSarathiDutta/youtube-transcript-tool.git
cd youtube-transcript-tool
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Optional TranscriptAPI setup

TranscriptAPI is **optional** and used only when earlier methods fail.

```bash
cp .env.example .env
```

Edit `.env`:

```env
TRANSCRIPT_API_KEY=your_api_key_here
```

Get a key at [transcriptapi.com/signup](https://transcriptapi.com/signup). Never commit `.env`.

## Usage

**Single video:**

```bash
python main.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

**Playlist:**

```bash
python main.py "https://www.youtube.com/playlist?list=PLAYLIST_ID"
```

Progress is printed to the terminal, for example:

```text
[3/13] Processing: Example Video Title
```

## Output

**Single video** → `<video_title>_transcript.docx`

**Playlist** → `<playlist_title>_transcripts.docx`

Example playlist document structure (placeholder text only):

```text
Playlist Title

Video 1: Example Lesson Title
YouTube URL: https://www.youtube.com/watch?v=EXAMPLE_ID

Transcript
First paragraph of cleaned caption text...

---

Video 2: Next Lesson Title
YouTube URL: https://www.youtube.com/watch?v=EXAMPLE_ID_2

Transcript
...
```

Page breaks are inserted between videos for readability.

## Project structure

```text
youtube-transcript-tool/
├── main.py
├── youtube_utils.py
├── transcript_service.py
├── document_writer.py
├── requirements.txt
├── .env.example
├── .gitignore
├── LICENSE
└── README.md
```

## Error handling

- Invalid URLs exit with a clear CLI message.
- Private, deleted, or unavailable playlist entries are skipped or included with metadata when possible.
- If a transcript cannot be retrieved, the document still lists that video with **`Transcript unavailable.`**
- Playlist processing continues after any single-video failure.

## Limitations

- Transcript availability depends on YouTube captions and third-party provider access.
- YouTube may rate-limit direct caption requests from some networks or IPs.
- Some private or deleted videos may not expose metadata or subtitles.
- Transcript quality reflects the underlying captions (manual vs auto-generated).
- TranscriptAPI usage consumes API credits on successful requests.

## Tech stack

- Python 3
- [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [python-docx](https://github.com/python-openxml/python-docx)
- [python-dotenv](https://github.com/theskumar/python-dotenv)
- [TranscriptAPI](https://transcriptapi.com/) REST API (optional fallback)

## Motivation

Long technical YouTube courses are easier to archive, search, study, and process when playlists are converted into structured, portable documents. This tool automates that workflow without downloading video files.

## Future improvements

- Markdown or PDF export
- Parallel transcript retrieval with rate limiting
- Search or summarization over playlist transcripts
- Optional inclusion of timestamps in exported documents

These items are **not** implemented in the current release.

## License

MIT — see [LICENSE](LICENSE).
