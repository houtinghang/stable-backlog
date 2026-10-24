# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the SQLite store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stable_backlog import mirror, store
from tests.conftest import InboxFactory
from tests.mailfactory import build_mail, failed_mail


def test_everything_round_trips(make_inbox: InboxFactory, tmp_path: Path) -> None:
    spam = build_mail("[PATCH 6.1.y] spam", "x\n", message_id="<spam@x>")
    mails = [
        failed_mail(),
        failed_mail(body="broken\n", message_id="broken@gregkh"),
        build_mail("[PATCH 6.6.y] x", "[ Upstream commit 1234567890ab ]\n", message_id=None),
        build_mail("Re: x", "hi\n", in_reply_to="<a@b>", references="<a@b> <c@d>"),
        spam,
    ]
    collected = mirror.collect(make_inbox(mails, removed=[spam]), max_count=10)
    assert collected.skipped  # the test covers skipped messages
    assert collected.removed  # ... and removal records
    db = tmp_path / "s.sqlite3"
    with store.open_store(db) as conn:
        store.save_collected(conn, collected)
    with store.open_store(db) as conn:
        assert store.load_collected(conn) == collected


def test_saving_replaces_the_snapshot(make_inbox: InboxFactory, tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    first = mirror.collect(make_inbox([failed_mail(message_id="one@gregkh")]), max_count=10)
    second = mirror.collect(make_inbox([failed_mail(message_id="two@gregkh")]), max_count=10)
    with store.open_store(db) as conn:
        store.save_collected(conn, first)
        store.save_collected(conn, second)
    with store.open_store(db) as conn:
        assert store.load_collected(conn) == second


def test_empty_store_fails_loudly(tmp_path: Path) -> None:
    with store.open_store(tmp_path / "s.sqlite3") as conn:
        with pytest.raises(store.StoreError, match="sync"):
            store.load_collected(conn)
        with pytest.raises(store.StoreError, match="sync"):
            store.load_queue(conn)


def test_snapshot_info_round_trips(tmp_path: Path) -> None:
    utc = timezone.utc
    info = store.SnapshotInfo(
        synced_at=datetime(2026, 10, 25, 12, 0, tzinfo=utc),
        offline=True,
        lore_newest=datetime(2026, 10, 24, 16, 47, tzinfo=utc),
        queue_newest=datetime(2026, 10, 24, 14, 21, 52, tzinfo=timezone(timedelta(hours=-4))),
    )
    db = tmp_path / "s.sqlite3"
    with store.open_store(db) as conn:
        store.save_snapshot_info(conn, info)
    with store.open_store(db) as conn:
        assert store.load_snapshot_info(conn) == info


def test_missing_snapshot_info_fails_loudly(tmp_path: Path) -> None:
    with (
        store.open_store(tmp_path / "s.sqlite3") as conn,
        pytest.raises(store.StoreError, match="sync"),
    ):
        store.load_snapshot_info(conn)
