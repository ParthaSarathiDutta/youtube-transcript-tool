"""Git operations for private archive sync (no tokens in URLs or logs)."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path


class ArchiveGitError(Exception):
    """Git archive operation failed."""


_SENSITIVE = re.compile(
    r"(x-access-token:[^\s@]+|Bearer\s+[^\s]+|basic\s+[A-Za-z0-9+/=]+|AUTHORIZATION:\s*(?:bearer|basic)\s+[^\s]+)",
    re.IGNORECASE,
)


def sanitize_git_message(text: str) -> str:
    if not text:
        return ""
    redacted = _SENSITIVE.sub("***", text)
    return redacted.strip()[:500]


_ARCHIVE_GIT_NAME = "youtube-transcript-tool"
_ARCHIVE_GIT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def _git_env(token: str | None) -> dict[str, str]:
    """Auth via GIT_CONFIG_* env vars so the token is not passed on the argv."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_AUTHOR_NAME"] = _ARCHIVE_GIT_NAME
    env["GIT_AUTHOR_EMAIL"] = _ARCHIVE_GIT_EMAIL
    env["GIT_COMMITTER_NAME"] = _ARCHIVE_GIT_NAME
    env["GIT_COMMITTER_EMAIL"] = _ARCHIVE_GIT_EMAIL
    if token:
        # GitHub HTTPS git expects Basic auth (x-access-token:PAT), not Bearer.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    return env


def archive_git_env(token: str | None) -> dict[str, str]:
    """Git environment for archive repo operations (auth + commit identity)."""
    return _git_env(token)


def _run_git(
    args: list[str],
    *,
    cwd: Path | None,
    token: str | None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git"] + args
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=_git_env(token),
    )
    if check and result.returncode != 0:
        detail = sanitize_git_message(result.stderr or result.stdout)
        raise ArchiveGitError(detail or "git command failed")
    return result


def clone_archive_repo(repo: str, dest: Path, token: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    url = f"https://github.com/{repo}.git"
    _run_git(["clone", "--depth", "1", url, str(dest)], cwd=None, token=token)


def pull_archive_repo(repo_dir: Path, token: str) -> None:
    _run_git(["pull", "--ff-only", "origin"], cwd=repo_dir, token=token)


def push_archive_repo(repo_dir: Path, token: str) -> None:
    _run_git(["push", "origin", "HEAD"], cwd=repo_dir, token=token)


def copy_remote_tree_into_archive(source: Path, archive_root: Path) -> None:
    """Merge remote clone contents into archive_root (preserves local .git-sync)."""
    archive_root.mkdir(parents=True, exist_ok=True)
    skip = {".git", ".git-sync"}
    for item in source.iterdir():
        if item.name in skip:
            continue
        dest = archive_root / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
