# SPDX-License-Identifier: GPL-2.0-or-later
"""Read stable-queue: queue-X.Y, staging-X.Y and releases/X.Y.Z.

A patch file counts for an upstream commit only if one of its marker lines
names it ("[ Upstream commit X ]", "commit X upstream.", "(cherry picked
from commit X)"); a hash mentioned in a Fixes: tag or in prose does not.
For queue-* and staging-* directories with a `series` file, only patches
listed there count (that is what quilt applies).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from stable_backlog import gitio
from stable_backlog.model import Location, QueueEntry
from stable_backlog.parse import MAX_MESSAGE_BYTES, find_upstreams

TREE_RE = re.compile(r"\d{1,2}\.\d{1,3}")
QUEUE_DIR_RE = re.compile(r"(?P<kind>queue|staging)-(?P<tree>\d{1,2}\.\d{1,3})")
RELEASE_DIR_RE = re.compile(r"(?P<tree>\d{1,2}\.\d{1,3})\.\d{1,4}")
PATCH_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+,=@-]{0,250}\.patch")


@dataclass(frozen=True)
class QueueScan:
    rev: str
    entries: tuple[QueueEntry, ...]
    patches_read: int
    without_marker: int
    not_in_series: int


def _names(repo: Path, spec: str) -> list[str]:
    out = gitio.run(repo, ["ls-tree", "-z", "--name-only", spec])
    return [n.decode("utf-8", "replace") for n in out.split(b"\0") if n]


def _directories(repo: Path, rev: str, trees: set[str]) -> list[tuple[str, str, Location]]:
    """(directory, tree, location) for every directory we need to read."""
    top = _names(repo, rev)
    dirs: list[tuple[str, str, Location]] = []
    for name in top:
        m = QUEUE_DIR_RE.fullmatch(name)
        if m and m.group("tree") in trees:
            dirs.append(
                (name, m.group("tree"), "queue" if m.group("kind") == "queue" else "staging")
            )
    if "releases" in top:
        for name in _names(repo, f"{rev}:releases"):
            m = RELEASE_DIR_RE.fullmatch(name)
            if m and m.group("tree") in trees:
                dirs.append((f"releases/{name}", m.group("tree"), "release"))
    return dirs


def _blobs(repo: Path, specs: list[str]) -> list[bytes | None]:
    """Contents of `rev:path` objects via one cat-file process (None if too big)."""
    proc = gitio.popen(repo, ["cat-file", "--batch"])
    if proc.stdin is None or proc.stdout is None:
        raise gitio.GitError("git cat-file started without pipes")
    out: list[bytes | None] = []
    try:
        for spec in specs:
            proc.stdin.write(spec.encode("utf-8") + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().decode("utf-8", "replace").split()
            if len(header) != 3 or header[1] != "blob":
                raise gitio.GitError(
                    f"stable-queue object {spec!r} unreadable: {' '.join(header)}"
                )
            size = int(header[2])
            data = proc.stdout.read(size + 1)
            if len(data) != size + 1:
                raise gitio.GitError("git cat-file ended early")
            out.append(data[:-1] if size <= MAX_MESSAGE_BYTES else None)
    finally:
        proc.stdin.close()
        proc.wait(timeout=60)
        proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    return out


def scan(repo: Path, rev: str, trees: Iterable[str]) -> QueueScan:
    """Find upstream commits in the queue, staging and release directories."""
    gitio.validate_full_hash(rev)
    wanted = set(trees)
    for tree in wanted:
        if not TREE_RE.fullmatch(tree):
            raise ValueError(f"not a stable tree: {tree!r:.40}")
    files: list[tuple[str, str, str, Location]] = []  # (path, directory, tree, location)
    not_in_series = 0
    for directory, tree, location in _directories(repo, rev, wanted):
        names = _names(repo, f"{rev}:{directory}")
        patches = [n for n in names if PATCH_NAME_RE.fullmatch(n)]
        if location != "release" and "series" in names:
            (series,) = _blobs(repo, [f"{rev}:{directory}/series"])
            listed = set((series or b"").decode("utf-8", "replace").split())
            not_in_series += sum(1 for p in patches if p not in listed)
            patches = [p for p in patches if p in listed]
        files += [(f"{directory}/{p}", directory, tree, location) for p in patches]

    contents = _blobs(repo, [f"{rev}:{path}" for path, *_ in files])
    entries: list[QueueEntry] = []
    without_marker = 0
    for (path, directory, tree, location), data in zip(files, contents, strict=True):
        text = (data or b"").decode("utf-8", "replace").split("\ndiff --git ", 1)[0]
        hashes = find_upstreams(text)
        if not hashes:
            without_marker += 1
        release = directory.removeprefix("releases/") if location == "release" else None
        entries += [QueueEntry(h, tree, location, directory, path, release) for h in hashes]
    entries.sort(key=lambda e: (e.tree, e.directory, e.path, e.upstream))
    return QueueScan(rev, tuple(entries), len(files), without_marker, not_in_series)
