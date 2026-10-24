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
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import pytest

from tests.mailfactory import GOLDEN, build_mail, failed_mail

INBOX_IMPORTED = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)  # first committer date
QUEUE_COMMITTED = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


class InboxFactory(Protocol):
    def __call__(self, messages: Iterable[bytes], removed: Iterable[bytes] = ()) -> Path: ...


class QueueFactory(Protocol):
    def __call__(self, src: Path, when: datetime = QUEUE_COMMITTED) -> tuple[Path, str]: ...


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


def commit_env(when: datetime) -> dict[str, str]:
    """Environment for a test commit with a fixed author and date."""
    return {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_AUTHOR_DATE": when.isoformat(),
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_DATE": when.isoformat(),
    }


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
        base = INBOX_IMPORTED
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
    """Return a factory that commits a directory tree as a stable-queue repo.

    The commit date is fixed (`when`), so tests never depend on the clock.
    """
    counter = iter(range(1000))

    def factory(src: Path, when: datetime = QUEUE_COMMITTED) -> tuple[Path, str]:
        repo = tmp_path / f"stable-queue-{next(counter)}"
        shutil.copytree(src, repo)
        _git(repo, "init", "--quiet", "-b", "master")
        _git(repo, "add", "-A")
        _git(repo, "commit", "--quiet", "-m", "snapshot", env=commit_env(when))
        return repo, _git(repo, "rev-parse", "HEAD")

    return factory


@pytest.fixture
def golden_mails() -> list[bytes]:
    return [p.read_bytes() for p in sorted((GOLDEN / "mail").glob("*.eml"))]


# --- the sync sources, pointed at local repositories ----------------------------

LORE_URL = "https://lore.kernel.org/stable/git/2.git"
NEXT_EPOCH_URL = "https://lore.kernel.org/stable/git/3.git"
QUEUE_URL = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/stable-queue.git"

FIRST = failed_mail(message_id="first@gregkh")
# Imported after FIRST (newer Date:), so an inbox of [FIRST, SECOND] extends [FIRST].
SECOND = build_mail(
    "[PATCH 6.6.y] tty: port: fix tty_port_tty_vhangup() race",
    "[ Upstream commit b48820de8c1a596c5b99775524dfce73c6576c5d ]\n",
    message_id="<second@example.org>",
    date="Sat, 24 Oct 2026 13:00:00 +0000",
)


class Remotes:
    """Point the fixed source URLs at local repositories, for git only.

    git's own url.<base>.insteadOf (in GIT_CONFIG_* variables) rewrites the
    real https URLs, so the code under test runs its real commands. A dead
    proxy makes any https request that is not rewritten fail at once.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self._targets: dict[str, Path] = {}
        self._apply()

    def point(self, url: str, repo: Path) -> None:
        self._targets[url] = repo
        self._apply()

    def clear(self) -> None:
        self._targets.clear()
        self._apply()

    def _apply(self) -> None:
        items = [("protocol.file.allow", "always"), ("http.proxy", "http://127.0.0.1:9")]
        items += [(f"url.{repo.as_uri()}.insteadOf", url) for url, repo in self._targets.items()]
        self._monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(items)))
        for i, (key, value) in enumerate(items):
            self._monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", key)
            self._monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", value)


@pytest.fixture
def remotes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Remotes:
    r = Remotes(monkeypatch)
    r.point(NEXT_EPOCH_URL, tmp_path / "no-such-epoch.git")
    return r


@pytest.fixture
def lore_remote(make_inbox: InboxFactory, remotes: Remotes) -> Path:
    """lore epoch 2, holding one FAILED report."""
    repo = make_inbox([FIRST])
    remotes.point(LORE_URL, repo)
    return repo


@pytest.fixture
def queue_remote(make_queue: QueueFactory, remotes: Remotes) -> Path:
    """stable-queue, holding the golden queue files."""
    repo, _ = make_queue(GOLDEN / "stable-queue")
    remotes.point(QUEUE_URL, repo)
    return repo


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"
