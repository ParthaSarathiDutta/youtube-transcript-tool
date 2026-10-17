"""Workflow automation: 7 AM Chicago schedule and manual dry-run defaults."""

from __future__ import annotations

from pathlib import Path


def test_workflow_schedule_prepared_not_active():
    text = Path(".github/workflows/daily-youtube-digest.yml").read_text(encoding="utf-8")
    assert "0 7 * * *" in text
    assert "America/Chicago" in text
    assert "# schedule:" in text or "#   - cron:" in text
    assert 'github.event_name' in text and "schedule" in text
    assert "workflow_dispatch" in text
    assert 'github.event.inputs.dry_run || \'true\'' in text or "dry_run || 'true'" in text
