# Deployment checklist

## GitHub Secrets (repository: `youtube-transcript-tool`)

| Secret | Required for | Notes |
|--------|----------------|-------|
| `YOUTUBE_API_KEY` | Channel discovery | YouTube Data API v3 key |
| `OPENAI_API_KEY` | Real summaries | |
| `OPENAI_MODEL` | Optional | Default `gpt-4o-mini` if unset |
| `ARCHIVE_GITHUB_REPO` | Private archive | `owner/youtube-ai-learning-archive` |
| `ARCHIVE_GITHUB_TOKEN` | Pull/push archive | Fine-grained PAT: contents read/write on archive repo only |
| `EMAIL_SMTP_HOST` | Delivery | e.g. `smtp.gmail.com` |
| `EMAIL_SMTP_PORT` | Delivery | e.g. `587` |
| `EMAIL_SMTP_USER` | Delivery | |
| `EMAIL_SMTP_PASSWORD` | Delivery | App password, not account password |
| `EMAIL_FROM` | Delivery | |
| `EMAIL_TO` | Delivery | Your inbox |

Optional repo variables: `YOUTUBE_CHANNEL_HANDLE`, `DISCOVERY_LOOKBACK_HOURS`, `DISCOVERY_RECOVERY_LOOKBACK_DAYS`.

## Private archive repository

1. Create empty private repo `youtube-ai-learning-archive`.
2. Add `index.json` at root: `{"videos": {}}`.
3. Grant PAT access only to this repo.

Each run **pulls** the archive at start (`sync_archive_from_github`) then **pushes** after successful non-dry-run processing.

Tokens are passed via Git `GIT_CONFIG_*` environment variables (not git argv, not HTTPS URLs).
Process listings may still briefly expose env on some hosts; use a fine-grained PAT limited to the archive repo.

## Enable daily schedule

1. Merge workflow `.github/workflows/daily-youtube-digest.yml` to **default branch** (`main`).
2. Run **workflow_dispatch** with `dry_run=true` and confirm transcript smoke passes on `ubuntu-latest`.
3. Uncomment the `schedule` block (7 AM `America/Chicago`).
4. Set workflow `dry_run` input default to `false` only after one successful manual send you approve.

## Email sent but archive push failed (manual recovery)

This can happen on ephemeral GitHub Actions runners: SMTP accepts the message,
but the second archive push (which persists `email_status: sent`) fails.

The pipeline sets `email_status` to `sent_archive_push_failed` and **does not**
treat the next scheduled run as safe to resend email.

Recovery (do not use `--force` for routine runs):

1. Confirm the digest email is already in your inbox.
2. Fix the archive push issue (token scope, repo name, network).
3. Locally or in Actions with secrets, run a **dry-run-free** push path:
   - Ensure `archive/YYYY/MM/DD/manifest.json` has `"email_status": "sent"`
     and `email_sent_at_utc` set.
   - Run `python run_daily.py --dry-run --video-id ...` is NOT enough; push requires
     `ARCHIVE_*` env and non-dry-run: fix manifest on disk, then
     `python -c` or re-run pipeline with `--send-email` blocked by skip logic once
     manifest is `"sent"` on the **remote** after a successful push.
4. Simplest: edit the private archive repo manifest to `"email_status": "sent"`
   via GitHub UI, then verify the next run skips email.

Never routine-schedule with `--force`.

## Local verification commands

```bash
pytest -v
python discover.py --dry-run
python run_daily.py --dry-run --mock-llm --force --video-id VIDEO_ID
python run_daily.py --send-email --force --video-id VIDEO_ID   # only after SMTP configured + you approve
```
