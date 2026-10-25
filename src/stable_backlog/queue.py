# SPDX-License-Identifier: GPL-2.0-or-later
"""Read stable-queue: queue-X.Y, staging-X.Y and releases/X.Y.Z.

A patch file counts for an upstream commit only if one of its marker lines
names it ("[ Upstream commit X ]", "commit X upstream.", "(cherry picked
from commit X)"); a hash mentioned in a Fixes: tag or in prose does not.
For queue-* and staging-* directories with a `series` file, only patches
listed there count (that is what quilt applies).

The tree is listed once (`git ls-tree -r`) and every file is read by its
blob id. Asking git for `rev:path` instead makes it search the releases/
tree (4,000+ entries) again for each of the 200,000 patches: two minutes
instead of about fifteen seconds. A blob shared by several files is read
once, and only the upstream commits found in it are kept.

Only the first MAX_MESSAGE_BYTES of a patch are searched: the markers are
at the top, and the limit bounds the work for a huge patch (the largest,
6.9 MB in October 2026, is a big diff). A `series` file over the limit is
an error rather than a partial list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from stable_backlog import gitio
from stable_backlog.model import Location, QueueEntry
from stable_backlog.parse import MAX_MESSAGE_BYTES, find_upstreams

TREE_RE = re.compile(r"\d{1,2}\.\d{1,3}")
QUEUE_DIR_RE = re.compile(r"(?P<kind>queue|staging)-(?P<tree>\d{1,2}\.\d{1,3})")
RELEASE_DIR_RE = re.compile(r"(?P<tree>\d{1,2}\.\d{1,3})\.\d{1,4}")
PATCH_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+,=@-]{0,250}\.patch")
SERIES = "series"
_CHUNK = 1 << 16


@dataclass(frozen=True)
class QueueScan:
    rev: str
    entries: tuple[QueueEntry, ...]
    patches_read: int
    without_marker: int
    not_in_series: int


@dataclass(frozen=True)
class _Entry:
    """A direct child of a directory we read, from `git ls-tree`."""

    directory: str
    tree: str
    location: Location
    name: str
    kind: str  # git object type: "blob" for a file, "tree", "commit" (a submodule)
    blob: str  # object id

    @property
    def path(self) -> str:
        return f"{self.directory}/{self.name}"


def _place(path: str) -> tuple[str, str, Location, str] | None:
    """(directory, tree, location, name) if path is directly in a directory we read."""
    parts = path.split("/")
    if len(parts) == 2:
        m = QUEUE_DIR_RE.fullmatch(parts[0])
        if m:
            location: Location = "queue" if m.group("kind") == "queue" else "staging"
            return parts[0], m.group("tree"), location, parts[1]
    elif len(parts) == 3 and parts[0] == "releases":
        m = RELEASE_DIR_RE.fullmatch(parts[1])
        if m:
            return f"releases/{parts[1]}", m.group("tree"), "release", parts[2]
    return None


def _listing(repo: Path, rev: str, trees: set[str]) -> dict[str, list[_Entry]]:
    """The entries of every directory we read, by directory, from one `ls-tree`."""
    out = gitio.run(repo, ["ls-tree", "-r", "-t", "-z", rev])
    dirs: dict[str, list[_Entry]] = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        info, tab, path = record.partition(b"\t")
        fields = info.decode("ascii", "replace").split()
        if not tab or len(fields) != 3:
            raise gitio.GitError(
                f"stable-queue {rev[:12]}: unexpected ls-tree output {record[:80]!r}"
            )
        place = _place(path.decode("utf-8", "replace"))
        if place is None or place[1] not in trees:
            continue
        directory, tree, location, name = place
        blob = gitio.validate_full_hash(fields[2])
        dirs.setdefault(directory, []).append(
            _Entry(directory, tree, location, name, fields[1], blob)
        )
    return dirs


def _file(entry: _Entry, rev: str) -> _Entry:
    if entry.kind != "blob":
        raise gitio.GitError(
            f"stable-queue {rev[:12]}: {entry.path!r} is a {entry.kind}, not a file"
        )
    return entry


def _discard(stream: IO[bytes], n: int) -> None:
    while n > 0:
        chunk = stream.read(min(n, _CHUNK))
        if not chunk:
            raise gitio.GitError("git cat-file ended early")
        n -= len(chunk)


def _read_blobs(repo: Path, rev: str, blobs: dict[str, str]) -> Iterator[tuple[str, int, bytes]]:
    """(blob id, size, first MAX_MESSAGE_BYTES of the content) of each blob.

    One `git cat-file --batch`, one blob at a time, so nothing piles up;
    `blobs` maps each blob id to a path that has it, for error messages.
    """
    if not blobs:
        return
    proc = gitio.popen(repo, ["cat-file", "--batch"])
    if proc.stdin is None or proc.stdout is None:
        raise gitio.GitError("git cat-file started without pipes")
    try:
        for blob, path in blobs.items():
            proc.stdin.write(f"{blob}\n".encode("ascii"))
            proc.stdin.flush()
            header = proc.stdout.readline().decode("ascii", "replace").split()
            if len(header) != 3 or header[0] != blob or header[1] != "blob":
                got = " ".join(header) or "git cat-file ended early"
                raise gitio.GitError(f"stable-queue {rev[:12]}: cannot read {path!r}: {got}")
            size = int(header[2])
            keep = min(size, MAX_MESSAGE_BYTES)
            data = proc.stdout.read(keep)
            if len(data) != keep:
                raise gitio.GitError(f"git cat-file ended early in {path!r}")
            _discard(proc.stdout, size - keep + 1)  # the rest and the newline
            yield blob, size, data
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()


def _patches(repo: Path, rev: str, dirs: dict[str, list[_Entry]]) -> tuple[list[_Entry], int]:
    """The patch files to read, and how many patches their series file leaves out."""
    series = {
        e.directory: _file(e, rev)
        for entries in dirs.values()
        for e in entries
        if e.location != "release" and e.name == SERIES
    }
    paths = {e.blob: e.path for e in series.values()}
    texts: dict[str, bytes] = {}
    for blob, size, data in _read_blobs(repo, rev, paths):
        if size > MAX_MESSAGE_BYTES:
            raise gitio.GitError(
                f"stable-queue {rev[:12]}: {paths[blob]!r} is too large ({size} bytes)"
            )
        texts[blob] = data
    patches: list[_Entry] = []
    not_in_series = 0
    for directory, entries in dirs.items():
        named = [e for e in entries if PATCH_NAME_RE.fullmatch(e.name)]
        if directory in series:
            listed = set(texts[series[directory].blob].decode("utf-8", "replace").split())
            not_in_series += sum(1 for e in named if e.name not in listed)
            named = [e for e in named if e.name in listed]
        patches += [_file(e, rev) for e in named]
    return patches, not_in_series


def _markers(data: bytes) -> tuple[str, ...]:
    return find_upstreams(data.decode("utf-8", "replace").split("\ndiff --git ", 1)[0])


def scan(repo: Path, rev: str, trees: Iterable[str]) -> QueueScan:
    """Find upstream commits in the queue, staging and release directories."""
    gitio.validate_full_hash(rev)
    wanted = set(trees)
    for tree in wanted:
        if not TREE_RE.fullmatch(tree):
            raise ValueError(f"not a stable tree: {tree!r:.40}")
    patches, not_in_series = _patches(repo, rev, _listing(repo, rev, wanted))
    found = {
        blob: _markers(data)
        for blob, _size, data in _read_blobs(repo, rev, {p.blob: p.path for p in patches})
    }
    entries: list[QueueEntry] = []
    without_marker = 0
    for p in patches:
        hashes = found[p.blob]
        if not hashes:
            without_marker += 1
        release = p.directory.removeprefix("releases/") if p.location == "release" else None
        entries += [
            QueueEntry(h, p.tree, p.location, p.directory, p.path, release) for h in hashes
        ]
    entries.sort(key=lambda e: (e.tree, e.directory, e.path, e.upstream))
    return QueueScan(rev, tuple(entries), len(patches), without_marker, not_in_series)
