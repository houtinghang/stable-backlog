# SPDX-License-Identifier: GPL-2.0-or-later
"""Build parsed records and item states directly, without git or mail.

States go through status.compute, so the tests see the same items the
commands do. Dates are counted back from REF, the newest lore message.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from stable_backlog import mirror, queue, status
from stable_backlog.model import (
    BackportPost,
    Envelope,
    FailedRequest,
    Location,
    OtherMail,
    QueueEntry,
)

REF = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)  # newest lore message


def env(mid: str | None, *, days_ago: float | None = 10, reply_to: str | None = None) -> Envelope:
    date = None if days_ago is None else REF - timedelta(days=days_ago)
    return Envelope(mid, "s", "someone@example.org", date, reply_to, ())


def failed(
    upstream: str,
    tree: str,
    mid: str,
    *,
    days_ago: float | None = 10,
    title: str = "foo: fix the bar",
    author: tuple[str, str] = ("Some Author", "author@example.org"),
    files: Sequence[str] = ("drivers/foo/bar.c",),
) -> FailedRequest:
    return FailedRequest(env(mid, days_ago=days_ago), upstream, tree, title, *author, tuple(files))


def backport(tree: str, upstream: str | None, mid: str, title: str = "x") -> BackportPost:
    return BackportPost(env(mid), (tree,), (upstream,) if upstream else (), title)


def collected(
    failed_: Sequence[FailedRequest],
    backports: Sequence[BackportPost] = (),
    others: Sequence[OtherMail] = (),
    skipped: Sequence[mirror.Skipped] = (),
) -> mirror.Collected:
    return mirror.Collected(
        "0" * 40, 0, tuple(failed_), tuple(backports), (), tuple(others), tuple(skipped)
    )


def scan(entries: Sequence[QueueEntry] = ()) -> queue.QueueScan:
    return queue.QueueScan("0" * 40, tuple(entries), 0, 0, 0)


def compute(
    failed_: Sequence[FailedRequest],
    backports: Sequence[BackportPost] = (),
    others: Sequence[OtherMail] = (),
    entries: Sequence[QueueEntry] = (),
) -> tuple[status.ItemState, ...]:
    return status.compute(collected(failed_, backports, others), scan(entries))


def entry(upstream: str, tree: str, directory: str) -> QueueEntry:
    release = directory.removeprefix("releases/") if directory.startswith("releases/") else None
    location: Location = "release" if release else "queue"
    return QueueEntry(upstream, tree, location, directory, f"{directory}/a.patch", release)
