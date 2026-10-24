# SPDX-License-Identifier: GPL-2.0-or-later
"""Read a public-inbox v2 epoch repository (e.g. lore stable/git/2.git).

In a v2 epoch every commit holds one message as the blob `m`. Commits are in
import order, so `git log` lists the newest first; the committer date is the
import time, while the author date is the message's own Date: header and is
not monotonic (never use `git log --since` on it).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from stable_backlog import gitio
from stable_backlog.model import BackportPost, FailedRequest, OtherMail, ReviewMail
from stable_backlog.parse import MAX_MESSAGE_BYTES, ParseError, parse_message

log = logging.getLogger(__name__)

_CHUNK = 1 << 20


class MirrorError(RuntimeError):
    """The mirror is unusable (missing objects, unexpected layout)."""


@dataclass(frozen=True)
class RawMessage:
    commit: str
    committed: datetime  # committer date = import time
    size: int
    raw: bytes | None  # None when skipped (too large)
    removed: bool = False  # a removal record (`d`): this message was taken out


@dataclass(frozen=True)
class Skipped:
    commit: str
    message_id: str | None
    reason: str


@dataclass(frozen=True)
class Collected:
    mirror_rev: str
    messages_read: int
    failed: tuple[FailedRequest, ...]
    backports: tuple[BackportPost, ...]
    reviews: tuple[ReviewMail, ...]
    others: tuple[OtherMail, ...]
    skipped: tuple[Skipped, ...]
    removed: tuple[str, ...] = ()  # Message-IDs taken out of the archive


def head(repo: Path) -> str:
    """The commit id of HEAD, validated."""
    out = gitio.run(repo, ["rev-parse", "--verify", "HEAD^{commit}"])
    return gitio.validate_full_hash(out.decode("ascii", "replace").strip())


def _log(repo: Path, rev: str, max_count: int) -> list[tuple[str, datetime]]:
    out = gitio.run(repo, ["log", f"--max-count={max_count}", "--format=%H %cI", rev, "--"])
    commits = []
    for line in out.decode("ascii", "replace").splitlines():
        commit, _, when = line.partition(" ")
        gitio.validate_full_hash(commit)
        commits.append((commit, datetime.fromisoformat(when.replace("Z", "+00:00"))))
    return commits


def _read_exact(stream: IO[bytes], n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(min(n - len(buf), _CHUNK))
        if not chunk:
            raise MirrorError("git cat-file ended early")
        buf += chunk
    return bytes(buf)


def _discard(stream: IO[bytes], n: int) -> None:
    while n > 0:
        chunk = stream.read(min(n, _CHUNK))
        if not chunk:
            raise MirrorError("git cat-file ended early")
        n -= len(chunk)


def _blob_size(stdin: IO[bytes], stdout: IO[bytes], spec: str) -> int | None:
    """Ask `cat-file --batch` for spec; its size, or None if it is missing."""
    stdin.write(f"{spec}\n".encode("ascii"))
    stdin.flush()
    header = stdout.readline().decode("ascii", "replace").split()
    if len(header) == 2 and header[1] == "missing":
        return None
    if len(header) != 3 or header[1] != "blob":
        raise MirrorError(f"{spec}: unexpected object ({' '.join(header)})")
    return int(header[2])


def iter_raw(repo: Path, max_count: int, rev: str | None = None) -> Iterator[RawMessage]:
    """Yield the newest max_count messages, newest first, one at a time.

    A commit holds either `m` (a message) or, for a removal, `d` (the removed
    message); anything else means the mirror is not what we expect.
    """
    if max_count < 1:
        raise ValueError("max_count must be at least 1")
    rev = gitio.validate_full_hash(rev) if rev is not None else head(repo)
    commits = _log(repo, rev, max_count)
    proc = gitio.popen(repo, ["cat-file", "--batch"])
    if proc.stdin is None or proc.stdout is None:
        raise MirrorError("git cat-file started without pipes")
    try:
        for commit, committed in commits:
            removed = False
            size = _blob_size(proc.stdin, proc.stdout, f"{commit}:m")
            if size is None:
                removed = True
                size = _blob_size(proc.stdin, proc.stdout, f"{commit}:d")
            if size is None:
                raise MirrorError(f"commit {commit}: blob 'm' (or removal 'd') missing")
            if size > MAX_MESSAGE_BYTES:
                _discard(proc.stdout, size + 1)
                yield RawMessage(commit, committed, size, None, removed)
                continue
            raw = _read_exact(proc.stdout, size)
            _discard(proc.stdout, 1)  # trailing newline
            yield RawMessage(commit, committed, size, raw, removed)
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


def collect(repo: Path, max_count: int) -> Collected:
    """Parse the newest max_count messages; count everything that is skipped.

    Removal records come after (newer than) the message they remove, so the
    removed Message-IDs are known before the original message is reached.
    """
    rev = head(repo)
    failed: list[FailedRequest] = []
    backports: list[BackportPost] = []
    reviews: list[ReviewMail] = []
    others: list[OtherMail] = []
    skipped: list[Skipped] = []
    removed: dict[str, None] = {}  # ordered set of removed Message-IDs
    n = 0
    for msg in iter_raw(repo, max_count, rev):
        n += 1
        if msg.raw is None:
            skipped.append(Skipped(msg.commit, None, "too-large"))
            continue
        try:
            rec = parse_message(msg.raw)
        except ParseError as e:
            if msg.removed:
                _note_removal(removed, skipped, msg.commit, e.message_id)
            else:
                log.warning("skipped message in commit %s: %s", msg.commit, e.reason)
                skipped.append(Skipped(msg.commit, e.message_id, e.reason))
            continue
        mid = rec.envelope.message_id
        if msg.removed:
            _note_removal(removed, skipped, msg.commit, mid)
        elif mid is not None and mid in removed:
            continue  # taken out of the archive later
        elif isinstance(rec, FailedRequest):
            failed.append(rec)
        elif isinstance(rec, BackportPost):
            backports.append(rec)
        elif isinstance(rec, ReviewMail):
            reviews.append(rec)
        else:
            others.append(rec)
    return Collected(
        rev,
        n,
        tuple(failed),
        tuple(backports),
        tuple(reviews),
        tuple(others),
        tuple(skipped),
        tuple(removed),
    )


def _note_removal(
    removed: dict[str, None], skipped: list[Skipped], commit: str, message_id: str | None
) -> None:
    if message_id is None:
        skipped.append(Skipped(commit, None, "removal record without Message-ID"))
    else:
        removed[message_id] = None
