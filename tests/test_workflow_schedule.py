"""Workflow automation: Chicago schedule and manual dry-run defaults."""

from __future__ import annotations

from pathlib import Path


def test_workflow_dual_chicago_schedule_sends_email_on_cron():
    text = Path(".github/workflows/daily-youtube-digest.yml").read_text(encoding="utf-8")
    assert 'cron: "13 7 * * *"' in text
    assert 'cron: "43 7 * * *"' in text
    assert text.count("timezone: America/Chicago") >= 2
    assert "schedule:" in text
    assert "# schedule:" not in text.split("schedule:")[0]
    assert 'github.event_name' in text and "schedule" in text
    assert 'ARGS="--send-email"' in text or "ARGS=\"--send-email\"" in text
    assert "workflow_dispatch" in text
    assert "dry_run || 'true'" in text
    assert 'ENABLE_SUMMARIES: "false"' in text
