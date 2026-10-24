# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for reading stable-queue (queue-*, staging-*, releases/)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stable_backlog import queue
from stable_backlog.gitio import GitError
from tests.conftest import QueueFactory
from tests.mailfactory import GOLDEN
from tests.test_status import H1, H2, patch_file, write_queue

TTY = "b48820de8c1a596c5b99775524dfce73c6576c5d"


def test_golden_queue(make_queue: QueueFactory) -> None:
    repo, rev = make_queue(GOLDEN / "stable-queue")
    scan = queue.scan(repo, rev, ("6.1", "6.6", "6.12"))
    assert scan.rev == rev
    tty = sorted(
        (e.tree, e.location, e.directory, e.release) for e in scan.entries if e.upstream == TTY
    )
    assert tty == [
        ("6.1", "queue", "queue-6.1", None),
        ("6.12", "release", "releases/6.12.113", "6.12.113"),
        ("6.6", "release", "releases/6.6.160", "6.6.160"),
    ]


def test_tree_filter(make_queue: QueueFactory) -> None:
    repo, rev = make_queue(GOLDEN / "stable-queue")
    scan = queue.scan(repo, rev, ("5.10",))
    assert {e.tree for e in scan.entries} == {"5.10"}


def test_series_file_is_honoured(tmp_path: Path, make_queue: QueueFactory) -> None:
    src = write_queue(
        tmp_path / "src",
        {
            "queue-6.1/series": "a.patch\n",
            "queue-6.1/a.patch": patch_file(H1),
            "queue-6.1/b.patch": patch_file(H2),
        },
    )
    repo, rev = make_queue(src)
    scan = queue.scan(repo, rev, ("6.1",))
    assert [e.upstream for e in scan.entries] == [H1]
    assert scan.not_in_series == 1


def test_staging_counts_as_queue(tmp_path: Path, make_queue: QueueFactory) -> None:
    repo, rev = make_queue(write_queue(tmp_path / "src", {"staging-6.6/a.patch": patch_file(H1)}))
    (entry,) = queue.scan(repo, rev, ("6.6",)).entries
    assert (entry.location, entry.directory) == ("staging", "staging-6.6")


def test_hash_mentioned_outside_marker_is_not_an_entry(
    tmp_path: Path, make_queue: QueueFactory
) -> None:
    decoy = (
        "Subject: x\n\n[ Upstream commit " + H2 + " ]\n\n"
        f'Fixes: {H1[:12]} ("foo")\nThis reverts commit {H1}.\n'
        f"> [ Upstream commit {H1} ]\n---\n+commit {H1} upstream.\n"
    )
    repo, rev = make_queue(write_queue(tmp_path / "src", {"queue-6.1/a.patch": decoy}))
    assert [e.upstream for e in queue.scan(repo, rev, ("6.1",)).entries] == [H2]


def test_patch_without_marker_is_counted(tmp_path: Path, make_queue: QueueFactory) -> None:
    repo, rev = make_queue(
        write_queue(tmp_path / "src", {"queue-6.1/a.patch": "Subject: stable-only fix\n\nx\n"})
    )
    scan = queue.scan(repo, rev, ("6.1",))
    assert scan.entries == ()
    assert scan.without_marker == 1


def test_odd_directory_names_are_ignored(tmp_path: Path, make_queue: QueueFactory) -> None:
    files = {
        "queue-6.1$(touch PWNED)/a.patch": patch_file(H1),
        "releases/6.1.1;rm/a.patch": patch_file(H1),
        "queue-6.1/a.patch": patch_file(H2),
    }
    repo, rev = make_queue(write_queue(tmp_path / "src", files))
    scan = queue.scan(repo, rev, ("6.1",))
    assert [e.upstream for e in scan.entries] == [H2]
    assert not (tmp_path / "PWNED").exists()


def test_rejects_bad_rev(make_queue: QueueFactory) -> None:
    repo, _ = make_queue(GOLDEN / "stable-queue")
    with pytest.raises(ValueError, match="hash"):
        queue.scan(repo, "HEAD; rm -rf /", ("6.1",))


def test_missing_repo_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="nope"):
        queue.scan(tmp_path / "nope", "0" * 40, ("6.1",))
