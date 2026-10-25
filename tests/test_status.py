# SPDX-License-Identifier: GPL-2.0-or-later
"""Unit tests for deriving the state of each (upstream commit, stable tree)."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from stable_backlog import mirror, queue, status
from stable_backlog.model import (
    BackportPost,
    Confidence,
    Envelope,
    FailedRequest,
    QueueEntry,
    Status,
)
from tests.conftest import InboxFactory, QueueFactory
from tests.mailfactory import build_mail, failed_mail

H1 = "1111111111111111111111111111111111111111"
H2 = "2222222222222222222222222222222222222222"
TITLE = "foo: fix the bar"


def patch_file(upstream: str, form: str = "bracket") -> str:
    marker = {
        "bracket": f"[ Upstream commit {upstream} ]",
        "commit": f"commit {upstream} upstream.",
    }[form]
    return f"From 0 Mon Sep 17 00:00:00 2001\nSubject: x\n\n{marker}\n\nbody\n---\n"


def write_queue(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    for d in root.glob("queue-*"):
        if not (d / "series").exists():
            names = sorted(p.name for p in d.glob("*.patch"))
            (d / "series").write_text("".join(f"{n}\n" for n in names))
    return root


def run(
    tmp_path: Path,
    make_inbox: InboxFactory,
    make_queue: QueueFactory,
    mails: list[bytes],
    files: dict[str, str] | None = None,
    trees: tuple[str, ...] = ("6.1", "6.6"),
) -> dict[tuple[str, str], status.ItemState]:
    collected = mirror.collect(make_inbox(mails), max_count=100)
    src = write_queue(tmp_path / "q-src", files or {"README": "x\n"})
    repo, rev = make_queue(src)
    scan = queue.scan(repo, rev, trees)
    return {(s.upstream, s.tree): s for s in status.compute(collected, scan)}


def backport(tree: str, upstream: str | None, mid: str, **kw: str) -> bytes:
    body = f"[ Upstream commit {upstream} ]\n\nfix\n" if upstream else "fix\n"
    return build_mail(f"[PATCH {tree}.y] {TITLE}", body, message_id=f"<{mid}>", **kw)


def test_no_reply(tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory) -> None:
    states = run(tmp_path, make_inbox, make_queue, [failed_mail(H1, "6.1", "f1@gregkh", TITLE)])
    s = states[(H1, "6.1")]
    assert s.status is Status.FAILED
    assert s.confidence is None
    assert s.backports == ()
    assert s.replies == ()


def test_reply_without_backport_is_recorded(
    tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory
) -> None:
    mails = [
        failed_mail(H1, "6.1", "f1@gregkh", TITLE),
        build_mail(
            "Re: FAILED: ...",
            "not needed for 6.1\n",
            message_id="<r1@x>",
            in_reply_to="<f1@gregkh>",
        ),
    ]
    s = run(tmp_path, make_inbox, make_queue, mails)[(H1, "6.1")]
    assert s.status is Status.FAILED
    assert s.replies == ("r1@x",)


def test_backport_matched_by_hash_without_threading(
    tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory
) -> None:
    mails = [failed_mail(H1, "6.1", "f1@gregkh", TITLE), backport("6.1", H1, "b1@x")]
    s = run(tmp_path, make_inbox, make_queue, mails)[(H1, "6.1")]
    assert s.status is Status.BACKPORT_POSTED
    assert s.confidence is Confidence.HASH
    assert [m.post.envelope.message_id for m in s.backports] == ["b1@x"]


def test_backport_for_another_tree_does_not_count(
    tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory
) -> None:
    mails = [failed_mail(H1, "6.1", "f1@gregkh", TITLE), backport("6.6", H1, "b1@x")]
    s = run(tmp_path, make_inbox, make_queue, mails)[(H1, "6.1")]
    assert s.status is Status.FAILED


def test_short_hash_prefix_matches(
    tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory
) -> None:
    mails = [failed_mail(H1, "6.1", "f1@gregkh", TITLE), backport("6.1", H1[:12], "b1@x")]
    s = run(tmp_path, make_inbox, make_queue, mails)[(H1, "6.1")]
    assert s.status is Status.BACKPORT_POSTED
    assert s.confidence is Confidence.HASH


def test_title_fallback_is_low_confidence(
    tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory
) -> None:
    mails = [failed_mail(H1, "6.1", "f1@gregkh", TITLE), backport("6.1", None, "b1@x")]
    s = run(tmp_path, make_inbox, make_queue, mails)[(H1, "6.1")]
    assert s.status is Status.BACKPORT_POSTED
    assert s.confidence is Confidence.TITLE


def test_title_fallback_not_used_when_backport_names_another_commit(
    tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory
) -> None:
    mails = [failed_mail(H1, "6.1", "f1@gregkh", TITLE), backport("6.1", H2, "b1@x")]
    s = run(tmp_path, make_inbox, make_queue, mails)[(H1, "6.1")]
    assert s.status is Status.FAILED


def test_multi_tree_backport_matches_each_tree(
    tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory
) -> None:
    mails = [
        failed_mail(H1, "6.1", "f1@gregkh", TITLE),
        failed_mail(H1, "6.6", "f2@gregkh", TITLE),
        build_mail(f"[PATCH 6.1.y 6.6.y] {TITLE}", f"commit {H1} upstream.\n", message_id="<b@x>"),
    ]
    states = run(tmp_path, make_inbox, make_queue, mails)
    assert states[(H1, "6.1")].status is Status.BACKPORT_POSTED
    assert states[(H1, "6.6")].status is Status.BACKPORT_POSTED


def test_duplicate_failed_is_one_item(
    tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory
) -> None:
    mails = [
        failed_mail(H1, "6.1", "f1@gregkh", TITLE),
        failed_mail(H1, "6.1", "f2@gregkh", TITLE),
    ]
    states = run(tmp_path, make_inbox, make_queue, mails)
    assert len(states) == 1
    assert {f.envelope.message_id for f in states[(H1, "6.1")].failed} == {
        "f1@gregkh",
        "f2@gregkh",
    }


def test_queue_and_release_precedence(
    tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory
) -> None:
    mails = [
        failed_mail(H1, "6.1", "f1@gregkh", TITLE),
        failed_mail(H2, "6.1", "f2@gregkh", "other: thing"),
        failed_mail(H1, "6.6", "f3@gregkh", TITLE),
        backport("6.1", H1, "b1@x"),
    ]
    files = {
        "queue-6.1/a.patch": patch_file(H1),
        "releases/6.1.190/b.patch": patch_file(H2, "commit"),
        "staging-6.6/c.patch": patch_file(H1),
        "releases/6.6.160/c.patch": patch_file(H1),
    }
    states = run(tmp_path, make_inbox, make_queue, mails, files)
    assert states[(H1, "6.1")].status is Status.QUEUED
    assert states[(H2, "6.1")].status is Status.RELEASED
    assert states[(H2, "6.1")].release == "6.1.190"
    six = states[(H1, "6.6")]
    assert six.status is Status.RELEASED
    assert six.release == "6.6.160"
    assert {e.location for e in six.queue} == {"staging", "release"}


def test_hash_sharing_only_the_first_12_digits_does_not_match(
    tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory
) -> None:
    upstream = "abcdef123456" + "1" * 28
    near = "abcdef123456" + "9999"
    mails = [failed_mail(upstream, "6.1", "f1@gregkh", TITLE), backport("6.1", near, "b1@x")]
    files = {"queue-6.1/a.patch": patch_file(near)}
    s = run(tmp_path, make_inbox, make_queue, mails, files)[(upstream, "6.1")]
    assert s.status is Status.FAILED
    assert (s.backports, s.queue) == ((), ())


def test_backport_naming_the_commit_twice_matches_once(
    tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory
) -> None:
    body = f"[ Upstream commit {H1} ]\n\ncommit {H1[:12]} upstream.\n"
    mails = [
        failed_mail(H1, "6.1", "f1@gregkh", TITLE),
        build_mail(f"[PATCH 6.1.y] {TITLE}", body, message_id="<b1@x>"),
    ]
    s = run(tmp_path, make_inbox, make_queue, mails)[(H1, "6.1")]
    assert [m.post.envelope.message_id for m in s.backports] == ["b1@x"]


def _hash(n: int) -> str:
    return hashlib.sha1(str(n).encode(), usedforsecurity=False).hexdigest()


def test_compute_scales_to_a_full_archive() -> None:
    # The size of the real data (2026-10: 3,604 FAILED reports and 8,190
    # backports in 90 days of the list, 201,662 stable-queue patches): each
    # request must not scan every backport and queue entry of its tree.
    trees = ("5.10", "5.15", "6.1", "6.6", "6.12", "6.18", "7.1", "7.2")
    env = Envelope(None, "s", "a@example.org", None, None, ())
    failed = tuple(
        FailedRequest(env, _hash(i), trees[i % 8], f"t{i}", "", "", ()) for i in range(3600)
    )
    backports = tuple(
        BackportPost(env, (trees[i % 8],), (_hash(i if i % 3 == 0 else -i)[:12],), f"t{i}")
        for i in range(8200)
    )
    queue_entries = tuple(
        QueueEntry(_hash(i if i % 5 == 0 else -i), trees[i % 8], "release", "d", "p", "1.2.3")
        for i in range(200_000)
    )
    collected = mirror.Collected("0" * 40, 0, failed, backports, (), (), ())
    scan = queue.QueueScan("0" * 40, queue_entries, 0, 0, 0)
    start = time.monotonic()
    states = status.compute(collected, scan)
    assert time.monotonic() - start < 2.0
    by_status = {st: sum(1 for s in states if s.status is st) for st in Status}
    # i % 5 == 0: released; else i % 3 == 0: backport posted; else no backport
    assert by_status[Status.RELEASED] == 720
    assert by_status[Status.BACKPORT_POSTED] == 960
    assert by_status[Status.FAILED] == 1920


def test_release_of_another_tree_does_not_count(
    tmp_path: Path, make_inbox: InboxFactory, make_queue: QueueFactory
) -> None:
    mails = [failed_mail(H1, "6.1", "f1@gregkh", TITLE)]
    files = {"releases/6.6.160/a.patch": patch_file(H1), "queue-6.6/a.patch": patch_file(H1)}
    s = run(tmp_path, make_inbox, make_queue, mails, files)[(H1, "6.1")]
    assert s.status is Status.FAILED
