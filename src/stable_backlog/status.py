# SPDX-License-Identifier: GPL-2.0-or-later
"""Derive the state of each (upstream commit, stable tree) pair.

One item per pair that has at least one FAILED report:

    RELEASED  a patch for it is in stable-queue releases/X.Y.Z
    QUEUED    ... in queue-X.Y or staging-X.Y
    BACKPORT_POSTED  someone mailed a backport for that tree
    FAILED    none of the above was found ("no backport found")

The current stable-queue snapshot decides QUEUED / RELEASED, so a patch that
was dropped from the queue falls back to BACKPORT_POSTED by itself.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from stable_backlog.mirror import Collected
from stable_backlog.model import (
    BackportPost,
    Confidence,
    Envelope,
    FailedRequest,
    QueueEntry,
    Status,
)
from stable_backlog.parse import normalize_title
from stable_backlog.queue import QueueScan


@dataclass(frozen=True)
class BackportMatch:
    post: BackportPost
    confidence: Confidence


@dataclass(frozen=True)
class ItemState:
    upstream: str
    tree: str
    status: Status
    confidence: Confidence | None  # None when nothing was found
    failed: tuple[FailedRequest, ...]
    backports: tuple[BackportMatch, ...]
    replies: tuple[str, ...]  # Message-IDs of other messages in the FAILED threads
    queue: tuple[QueueEntry, ...]
    release: str | None  # first release that has it


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def _children(envelopes: Iterable[Envelope]) -> dict[str, set[str]]:
    """Message-ID -> Message-IDs of messages that thread below it."""
    out: dict[str, set[str]] = defaultdict(set)
    for env in envelopes:
        if env.message_id is None:
            continue
        parents = set(env.references)
        if env.in_reply_to:
            parents.add(env.in_reply_to)
        for parent in parents:
            out[parent].add(env.message_id)
    return out


def _matches(upstream: str, title: str, candidates: Iterable[BackportPost]) -> list[BackportMatch]:
    found = []
    want = normalize_title(title)
    for post in candidates:
        if any(upstream.startswith(h) for h in post.upstreams):
            found.append(BackportMatch(post, Confidence.HASH))
        elif not post.upstreams and want and normalize_title(post.title) == want:
            found.append(BackportMatch(post, Confidence.TITLE))
    return sorted(found, key=lambda m: (m.post.envelope.message_id or "", m.post.title))


def compute(collected: Collected, scan: QueueScan) -> tuple[ItemState, ...]:
    groups: dict[tuple[str, str], list[FailedRequest]] = defaultdict(list)
    for req in collected.failed:
        groups[(req.upstream, req.tree)].append(req)

    by_tree: dict[str, list[BackportPost]] = defaultdict(list)
    for post in collected.backports:
        for tree in post.trees:
            by_tree[tree].append(post)

    queue_by_tree: dict[str, list[QueueEntry]] = defaultdict(list)
    for entry in scan.entries:
        queue_by_tree[entry.tree].append(entry)

    children = _children(
        [p.envelope for p in collected.backports]
        + [o.envelope for o in collected.others]
        + [r.envelope for r in collected.reviews]
    )

    states = []
    for (upstream, tree), reqs in sorted(groups.items()):
        reqs.sort(key=lambda r: r.envelope.message_id or "")
        matches = _matches(upstream, reqs[0].title, by_tree.get(tree, ()))
        queue = tuple(e for e in queue_by_tree.get(tree, ()) if upstream.startswith(e.upstream))
        released = sorted((e.release for e in queue if e.release), key=_version_key)
        matched_ids = {m.post.envelope.message_id for m in matches}
        replies = sorted(
            {c for r in reqs for c in children.get(r.envelope.message_id or "", ())}
            - {m for m in matched_ids if m}
        )
        if released:
            status = Status.RELEASED
        elif queue:
            status = Status.QUEUED
        elif matches:
            status = Status.BACKPORT_POSTED
        else:
            status = Status.FAILED
        if queue or any(m.confidence is Confidence.HASH for m in matches):
            confidence: Confidence | None = Confidence.HASH
        elif matches:
            confidence = Confidence.TITLE
        else:
            confidence = None
        states.append(
            ItemState(
                upstream,
                tree,
                status,
                confidence,
                tuple(reqs),
                tuple(matches),
                tuple(replies),
                queue,
                released[0] if released else None,
            )
        )
    return tuple(states)
