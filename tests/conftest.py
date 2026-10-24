# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared fixtures: build small public-inbox v2 and stable-queue git repos.

Everything is created under tmp_path from files in tests/fixtures; tests never
touch the network or a real mirror.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import pytest

from tests.mailfactory import GOLDEN


class InboxFactory(Protocol):
    def __call__(self, messages: Iterable[bytes], removed: Iterable[bytes] = ()) -> Path: ...


QueueFactory = Callable[[Path], tuple[Path, str]]


def _git(
    cwd: Path, *args: str, stdin: bytes | None = None, env: dict[str, str] | None = None
) -> str:
    out = subprocess.run(  # noqa: S603 - fixed git commands on test-owned repos
        [shutil.which("git") or "git", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        check=True,
        timeout=60,
        env={**os.environ, **(env or {})},
    )
    return out.stdout.decode().strip()


def _date_of(raw: bytes) -> datetime:
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    value = msg.get("Date")
    if value is None:
        return datetime(2026, 1, 1, tzinfo=timezone.utc)
    return email.utils.parsedate_to_datetime(str(value))


@pytest.fixture
def make_inbox(tmp_path: Path) -> InboxFactory:
    """Return a factory that imports raw messages into a v2-style epoch repo.

    Like public-inbox, every commit has a tree with a single blob `m` holding
    the raw message; messages are imported oldest first, so `git log` lists
    the newest first. The committer date is the import time. Messages in
    `removed` are then deleted the way public-inbox does it: a later commit
    whose tree holds the removed message as `d` instead of `m`.
    """
    counter = iter(range(1000))

    def factory(messages: Iterable[bytes], removed: Iterable[bytes] = ()) -> Path:
        repo = tmp_path / f"inbox-{next(counter)}.git"
        _git(tmp_path, "init", "--quiet", "--bare", str(repo))
        parent: str | None = None
        ordered = [("m", raw) for raw in sorted(messages, key=_date_of)]
        ordered += [("d", raw) for raw in removed]
        base = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        for i, (name, raw) in enumerate(ordered):
            blob = _git(repo, "hash-object", "-w", "--stdin", stdin=raw)
            tree = _git(repo, "mktree", stdin=f"100644 blob {blob}\t{name}\n".encode())
            when = (base + timedelta(seconds=i)).isoformat()
            env = {
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_AUTHOR_DATE": _date_of(raw).isoformat(),
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_DATE": when,
            }
            args = ["commit-tree", tree, "-m", f"message {i}"]
            if parent:
                args += ["-p", parent]
            parent = _git(repo, *args, env=env)
        if parent:
            _git(repo, "update-ref", "refs/heads/master", parent)
        return repo

    return factory


@pytest.fixture
def make_queue(tmp_path: Path) -> QueueFactory:
    """Return a factory that commits a directory tree as a stable-queue repo."""
    counter = iter(range(1000))

    def factory(src: Path) -> tuple[Path, str]:
        repo = tmp_path / f"stable-queue-{next(counter)}"
        shutil.copytree(src, repo)
        _git(repo, "init", "--quiet", "-b", "master")
        _git(repo, "add", "-A")
        env = {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        _git(repo, "commit", "--quiet", "-m", "snapshot", env=env)
        return repo, _git(repo, "rev-parse", "HEAD")

    return factory


@pytest.fixture
def golden_mails() -> list[bytes]:
    return [p.read_bytes() for p in sorted((GOLDEN / "mail").glob("*.eml"))]
