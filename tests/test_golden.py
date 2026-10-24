# SPDX-License-Identifier: GPL-2.0-or-later
"""Golden test: real FAILED requests with known outcomes.

Fixtures: tests/fixtures/golden (real lore messages and stable-queue files,
trimmed; provenance in MANIFEST.tsv). Expected states were checked by hand
against lore and stable-queue at the revisions named in MANIFEST.tsv.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stable_backlog import mirror, queue, status, store
from stable_backlog.model import Confidence, Status
from tests.conftest import InboxFactory, QueueFactory
from tests.mailfactory import GOLDEN

UPSTREAM = {
    "gso": "006c8b834aed8b02bd8fb9fb05e9e7ead37d1763",
    "pmd": "fe6cf984939d8e12cb33a99673c8d026c5135e68",
    "acpi": "f234fdaae1cad8c39265e7ca0a14633076ec7154",
    "tty": "b48820de8c1a596c5b99775524dfce73c6576c5d",
    "damon": "90179da203ba8b708c84a12a07cd44be0f346334",
}
TREES = ("5.10", "5.15", "6.1", "6.6", "6.12", "6.18")

# (case, tree, status, release or queue directory, fixtures of matched backports)
EXPECTED: list[tuple[str, str, Status, str | None, list[str]]] = [
    ("gso", "6.18", Status.BACKPORT_POSTED, None, ["gso-backport-6.18"]),
    ("gso", "6.12", Status.BACKPORT_POSTED, None, ["gso-backport-6.12"]),
    ("gso", "6.6", Status.BACKPORT_POSTED, None, ["gso-backport-6.6"]),
    ("gso", "6.1", Status.BACKPORT_POSTED, None, ["gso-backport-6.1"]),
    ("gso", "5.15", Status.BACKPORT_POSTED, None, ["gso-backport-5.15"]),
    ("gso", "5.10", Status.BACKPORT_POSTED, None, ["gso-backport-5.10"]),
    # 6.18: Sasha mailed a backport; 6.12 / 6.6: queued without any mail
    ("pmd", "6.18", Status.RELEASED, "6.18.52", ["pmd-backport-6.18-sasha"]),
    ("pmd", "6.12", Status.RELEASED, "6.12.114", []),
    ("pmd", "6.6", Status.RELEASED, "6.6.160", []),
    ("pmd", "6.1", Status.BACKPORT_POSTED, None, ["pmd-backport-6.1"]),
    ("pmd", "5.15", Status.BACKPORT_POSTED, None, ["pmd-backport-5.15"]),
    ("pmd", "5.10", Status.BACKPORT_POSTED, None, ["pmd-backport-5.10"]),
    # answered by patch 2/3 of a series whose cover letter replies to FAILED
    ("acpi", "6.18", Status.BACKPORT_POSTED, None, ["acpi-series-6.18-2"]),
    # 6.12 was queued from the 6.6.y backport ("Queued for 6.12 and 6.6")
    ("tty", "6.12", Status.RELEASED, "6.12.113", []),
    ("tty", "6.6", Status.RELEASED, "6.6.160", ["tty-backport-6.6"]),
    ("tty", "6.1", Status.QUEUED, "queue-6.1", ["tty-backport-6.1"]),
    ("tty", "5.15", Status.QUEUED, "queue-5.15", ["tty-backport-5.15-2"]),
    ("tty", "5.10", Status.QUEUED, "queue-5.10", ["tty-backport-5.10-2"]),
    # backport sent as a new thread, found by its "(cherry picked from ...)"
    ("damon", "6.18", Status.BACKPORT_POSTED, None, ["damon-backport-6.18-1"]),
]


def manifest_ids() -> dict[str, str]:
    """fixture name -> Message-ID (without angle brackets)."""
    out = {}
    for line in (GOLDEN / "MANIFEST.tsv").read_text().splitlines():
        if line.startswith("#"):
            continue
        name, _commit, mid = line.split("\t")
        if name.startswith("mail/"):
            out[name.removeprefix("mail/").removesuffix(".eml")] = mid.strip("<>")
    return out


@pytest.fixture
def golden_states(
    make_inbox: InboxFactory, make_queue: QueueFactory, golden_mails: list[bytes]
) -> tuple[mirror.Collected, queue.QueueScan, dict[tuple[str, str], status.ItemState]]:
    collected = mirror.collect(make_inbox(golden_mails), max_count=1000)
    repo, rev = make_queue(GOLDEN / "stable-queue")
    scan = queue.scan(repo, rev, TREES)
    states = {(s.upstream, s.tree): s for s in status.compute(collected, scan)}
    return collected, scan, states


def test_nothing_skipped_and_all_requests_found(
    golden_states: tuple[
        mirror.Collected, queue.QueueScan, dict[tuple[str, str], status.ItemState]
    ],
    golden_mails: list[bytes],
) -> None:
    collected, _scan, states = golden_states
    assert collected.messages_read == len(golden_mails) == 47
    assert collected.skipped == ()
    assert len(collected.failed) == 19
    assert len(collected.reviews) == 2
    assert set(states) == {(UPSTREAM[c], t) for c, t, *_ in EXPECTED}


@pytest.mark.parametrize(
    ("case", "tree", "expected", "where", "backports"),
    EXPECTED,
    ids=[f"{c}-{t}" for c, t, *_ in EXPECTED],
)
def test_golden_state(
    golden_states: tuple[
        mirror.Collected, queue.QueueScan, dict[tuple[str, str], status.ItemState]
    ],
    case: str,
    tree: str,
    expected: Status,
    where: str | None,
    backports: list[str],
) -> None:
    ids = manifest_ids()
    s = golden_states[2][(UPSTREAM[case], tree)]
    assert s.status is expected
    assert s.confidence is Confidence.HASH
    assert sorted(m.post.envelope.message_id or "" for m in s.backports) == sorted(
        ids[b] for b in backports
    )
    assert all(m.confidence is Confidence.HASH for m in s.backports)
    if expected is Status.RELEASED:
        assert s.release == where
    elif expected is Status.QUEUED:
        assert {e.directory for e in s.queue} == {where}
    else:
        assert s.queue == ()
    assert [f.envelope.message_id for f in s.failed] == [ids[f"{case}-failed-{tree}"]]


def test_thread_replies_are_kept(
    golden_states: tuple[
        mirror.Collected, queue.QueueScan, dict[tuple[str, str], status.ItemState]
    ],
) -> None:
    ids = manifest_ids()
    states = golden_states[2]
    assert ids["acpi-series-6.18-0"] in states[(UPSTREAM["acpi"], "6.18")].replies
    assert ids["damon-reply-6.18-sj"] in states[(UPSTREAM["damon"], "6.18")].replies


def test_store_round_trip(
    golden_states: tuple[
        mirror.Collected, queue.QueueScan, dict[tuple[str, str], status.ItemState]
    ],
    tmp_path: Path,
) -> None:
    collected, scan, states = golden_states
    db = tmp_path / "state.sqlite3"
    with store.open_store(db) as conn:
        store.save_collected(conn, collected)
        store.save_queue(conn, scan)
    with store.open_store(db) as conn:
        loaded, loaded_scan = store.load_collected(conn), store.load_queue(conn)
    assert loaded == collected
    assert loaded_scan == scan
    again = status.compute(loaded, loaded_scan)
    assert {(s.upstream, s.tree): s for s in again} == states
