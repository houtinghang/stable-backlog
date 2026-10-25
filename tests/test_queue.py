# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for reading stable-queue (queue-*, staging-*, releases/)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from stable_backlog import queue
from stable_backlog.gitio import GitError
from stable_backlog.parse import MAX_MESSAGE_BYTES
from tests.conftest import QueueFactory, _git
from tests.mailfactory import GOLDEN
from tests.test_status import H1, H2, patch_file, write_queue

TTY = "b48820de8c1a596c5b99775524dfce73c6576c5d"

# The shape of stable-queue on 2026-10-24 for the trees with FAILED reports:
# release directories per tree, the patches in them, and release directories
# in all (most of them for older trees).
REAL_RELEASE_DIRS = {
    "5.10": 270,
    "5.15": 220,
    "6.1": 190,
    "6.6": 158,
    "6.12": 114,
    "6.18": 58,
    "7.1": 13,
    "7.2": 12,
}
REAL_PATCHES = 201_662
REAL_ALL_RELEASE_DIRS = 4_378
# Reading every patch as `rev:path` took 97 s on this shape (2 minutes on the
# real stable-queue); reading them by blob id takes about 9 s.
REAL_SCALE_SECONDS = 30.0


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


def test_same_patch_in_two_directories_counts_in_both(
    tmp_path: Path, make_queue: QueueFactory
) -> None:
    files = {"queue-6.1/a.patch": patch_file(H1), "releases/6.1.5/a.patch": patch_file(H1)}
    scan = queue.scan(*make_queue(write_queue(tmp_path / "src", files)), ("6.1",))
    assert [(e.upstream, e.path) for e in scan.entries] == [
        (H1, "queue-6.1/a.patch"),
        (H1, "releases/6.1.5/a.patch"),
    ]
    assert scan.patches_read == 2


def test_patches_in_subdirectories_are_ignored(tmp_path: Path, make_queue: QueueFactory) -> None:
    files = {
        "releases/6.1.1/sub/a.patch": patch_file(H1),
        "queue-6.1/sub/b.patch": patch_file(H2),
        "queue-6.1/c.patch": patch_file(H2),
    }
    scan = queue.scan(*make_queue(write_queue(tmp_path / "src", files)), ("6.1",))
    assert [e.path for e in scan.entries] == ["queue-6.1/c.patch"]


@pytest.mark.parametrize(
    ("files", "match"),
    [
        ({"releases/6.1.1/x.patch/README": "x\n"}, r"releases/6\.1\.1/x\.patch"),
        (
            {"queue-6.1/a.patch": patch_file(H1), "queue-6.1/series/x": "a.patch\n"},
            r"queue-6\.1/series",
        ),
    ],
    ids=["patch-named-directory", "series-directory"],
)
def test_directory_where_a_file_belongs_fails_loudly(
    tmp_path: Path, make_queue: QueueFactory, files: dict[str, str], match: str
) -> None:
    repo, rev = make_queue(write_queue(tmp_path / "src", files))
    with pytest.raises(GitError, match=match):
        queue.scan(repo, rev, ("6.1",))


def test_missing_patch_object_fails_loudly(tmp_path: Path, make_queue: QueueFactory) -> None:
    repo, rev = make_queue(write_queue(tmp_path / "src", {"queue-6.1/a.patch": patch_file(H1)}))
    blob = _git(repo, "rev-parse", f"{rev}:queue-6.1/a.patch")
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    with pytest.raises(GitError, match=r"queue-6\.1/a\.patch"):
        queue.scan(repo, rev, ("6.1",))


def test_huge_patch_is_searched_from_its_start(tmp_path: Path, make_queue: QueueFactory) -> None:
    # Like releases/6.1.36/smb-move-client-and-server-files-to-common-directory-fs-smb.patch
    # (6.9 MB): the marker is at the top, the size is in the diff.
    huge = patch_file(H1) + "diff --git a/f b/f\n" + "+x\n" * (MAX_MESSAGE_BYTES // 3 + 1)
    repo, rev = make_queue(write_queue(tmp_path / "src", {"releases/6.1.36/a.patch": huge}))
    scan = queue.scan(repo, rev, ("6.1",))
    assert [e.upstream for e in scan.entries] == [H1]
    assert scan.without_marker == 0


def test_huge_series_file_fails_loudly(tmp_path: Path, make_queue: QueueFactory) -> None:
    series = "a.patch\n" + "# padding\n" * (MAX_MESSAGE_BYTES // 10 + 1)
    files = {"queue-6.1/series": series, "queue-6.1/a.patch": patch_file(H1)}
    repo, rev = make_queue(write_queue(tmp_path / "src", files))
    with pytest.raises(GitError, match=r"queue-6\.1/series.*too large"):
        queue.scan(repo, rev, ("6.1",))


def _numbered_patch(n: int) -> bytes:
    return (
        f"From: A U Thor <a@example.org>\nSubject: [PATCH] fix {n}\n\n"
        f"commit {n:040x} upstream.\n\nFix {n}.\n---\ndiff --git a/f{n}.c b/f{n}.c\n+y\n"
    ).encode()


def real_scale_queue(root: Path) -> tuple[Path, str, int]:
    """A stable-queue shaped like the real one, built with one `git fast-import`.

    Every patch is different and names its own upstream commit. Returns the
    repository, its commit and the number of patches scan() has to read.
    """
    files: list[tuple[str, bytes]] = []
    wanted = [f"{tree}.{n}" for tree, count in REAL_RELEASE_DIRS.items() for n in range(count)]
    per_dir, extra = divmod(REAL_PATCHES, len(wanted))
    for i, release in enumerate(wanted):
        for _ in range(per_dir + (i < extra)):
            files.append(
                (f"releases/{release}/fix-{len(files)}.patch", _numbered_patch(len(files)))
            )
    for tree in REAL_RELEASE_DIRS:
        names = [f"queued-{q}.patch" for q in range(30)]
        files += [
            (f"queue-{tree}/{name}", _numbered_patch(len(files) + i))
            for i, name in enumerate(names)
        ]
        files.append((f"queue-{tree}/series", "".join(f"{n}\n" for n in names).encode()))
        files.append((f"staging-{tree}/staged.patch", _numbered_patch(len(files))))
    to_read = len(files) - len(REAL_RELEASE_DIRS)  # all but the series files
    for k in range(REAL_ALL_RELEASE_DIRS - len(wanted)):  # other trees: listed, never read
        files.append((f"releases/4.{k // 400}.{k % 400}/old.patch", _numbered_patch(len(files))))
    stream = bytearray()
    for mark, (_, data) in enumerate(files, 1):
        stream += b"blob\nmark :%d\ndata %d\n%s\n" % (mark, len(data), data)
    stream += b"commit refs/heads/master\ncommitter test <test@example.invalid> 1761307200 +0000\n"
    stream += b"data 9\nsnapshot\n"
    for mark, (path, _) in enumerate(files, 1):
        stream += b"M 100644 :%d %s\n" % (mark, path.encode())
    repo = root / "stable-queue.git"
    _git(root, "init", "--quiet", "--bare", str(repo))
    _git(repo, "fast-import", "--quiet", stdin=bytes(stream))
    return repo, _git(repo, "rev-parse", "refs/heads/master"), to_read


@pytest.mark.slow
def test_scan_is_fast_at_real_scale(tmp_path: Path) -> None:
    repo, rev, to_read = real_scale_queue(tmp_path)
    start = time.monotonic()
    scan = queue.scan(repo, rev, tuple(REAL_RELEASE_DIRS))
    elapsed = time.monotonic() - start
    assert (scan.patches_read, len(scan.entries), scan.without_marker) == (to_read, to_read, 0)
    assert elapsed < REAL_SCALE_SECONDS, f"scan took {elapsed:.1f} s"


def test_rejects_bad_rev(make_queue: QueueFactory) -> None:
    repo, _ = make_queue(GOLDEN / "stable-queue")
    with pytest.raises(ValueError, match="hash"):
        queue.scan(repo, "HEAD; rm -rf /", ("6.1",))


def test_missing_repo_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="nope"):
        queue.scan(tmp_path / "nope", "0" * 40, ("6.1",))
