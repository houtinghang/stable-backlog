# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for reading a public-inbox v2 epoch repository."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from stable_backlog import mirror
from stable_backlog.gitio import GitError, git_executable
from tests.conftest import InboxFactory
from tests.mailfactory import build_mail, failed_mail


def numbered(n: int) -> list[bytes]:
    return [
        build_mail(
            f"[PATCH 6.1.y] m{i}",
            "x\n",
            message_id=f"<m{i}@x>",
            date=f"Sat, 24 Oct 2026 12:00:{i:02d} +0000",
        )
        for i in range(n)
    ]


def test_reads_newest_first_up_to_max_count(make_inbox: InboxFactory) -> None:
    result = mirror.collect(make_inbox(numbered(5)), max_count=3)
    assert result.messages_read == 3
    assert [b.envelope.message_id for b in result.backports] == ["m4@x", "m3@x", "m2@x"]


def test_records_mirror_revision(make_inbox: InboxFactory) -> None:
    repo = make_inbox(numbered(2))
    head = subprocess.run(  # noqa: S603 - fixed git command on a test repo
        [git_executable(), "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    assert mirror.collect(repo, max_count=10).mirror_rev == head


def test_iter_raw_reports_commit_and_import_time(make_inbox: InboxFactory) -> None:
    raws = list(mirror.iter_raw(make_inbox(numbered(2)), max_count=10))
    assert len(raws) == 2
    assert all(len(r.commit) == 40 for r in raws)
    assert raws[0].committed > raws[1].committed  # committer date = import time
    assert raws[0].raw is not None
    assert b"m1@x" in raws[0].raw


@pytest.mark.parametrize("count", [0, -1])
def test_invalid_max_count(count: int, make_inbox: InboxFactory) -> None:
    with pytest.raises(ValueError, match="max_count"):
        mirror.collect(make_inbox(numbered(1)), max_count=count)


def test_missing_repo_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="does-not-exist"):
        mirror.collect(tmp_path / "does-not-exist.git", max_count=10)


def test_commit_without_message_blob_fails_loudly(tmp_path: Path) -> None:
    repo = tmp_path / "broken.git"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }

    def git(*args: str, stdin: bytes | None = None) -> str:
        return (
            subprocess.run(  # noqa: S603 - fixed git commands on a test repo
                [git_executable(), "-C", str(repo), *args],
                input=stdin,
                capture_output=True,
                check=True,
                env=env,
            )
            .stdout.decode()
            .strip()
        )

    subprocess.run([git_executable(), "init", "-q", "--bare", str(repo)], check=True)  # noqa: S603
    blob = git("hash-object", "-w", "--stdin", stdin=b"not a message\n")
    tree = git("mktree", stdin=f"100644 blob {blob}\tnot-m\n".encode())
    commit = git("commit-tree", tree, "-m", "x")
    git("update-ref", "refs/heads/master", commit)
    with pytest.raises(mirror.MirrorError, match="missing"):
        mirror.collect(repo, max_count=10)


def test_removed_message_is_dropped_not_an_error(make_inbox: InboxFactory) -> None:
    # public-inbox records a removal (e.g. spam) as a commit holding `d`
    spam = build_mail("[PATCH 6.1.y] spam", "x\n", message_id="<spam@x>")
    keep = build_mail("[PATCH 6.1.y] keep", "x\n", message_id="<keep@x>")
    result = mirror.collect(make_inbox([spam, keep], removed=[spam]), max_count=10)
    assert [b.envelope.message_id for b in result.backports] == ["keep@x"]
    assert result.removed == ("spam@x",)
    assert result.skipped == ()
    assert result.messages_read == 3


def test_counts_kinds(make_inbox: InboxFactory) -> None:
    mails = [
        failed_mail(),
        build_mail("[PATCH 6.6.y] x", "fix\n", message_id="<b@x>"),
        build_mail("Re: something", "hi\n", message_id="<o@x>"),
    ]
    result = mirror.collect(make_inbox(mails), max_count=10)
    assert (len(result.failed), len(result.backports), len(result.others)) == (1, 1, 1)
    assert result.skipped == ()
