# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the SQLite store."""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stable_backlog import mirror, store
from tests.conftest import InboxFactory
from tests.mailfactory import build_mail, failed_mail


def test_everything_round_trips(make_inbox: InboxFactory, tmp_path: Path) -> None:
    spam = build_mail("[PATCH 6.1.y] spam", "x\n", message_id="<spam@x>")
    mails = [
        failed_mail(files=["drivers/a.c", "include/a.h"]),
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


def test_store_from_an_older_version_is_refused_with_a_hint(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    with (
        pytest.raises(store.StoreError, match=r"schema version 1.*stable-backlog sync"),
        store.open_store(db),
    ):
        pass


def test_open_snapshot_is_read_only(make_inbox: InboxFactory, tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    with store.open_store(db) as conn:
        store.save_collected(conn, mirror.collect(make_inbox([failed_mail()]), max_count=10))
    before = db.read_bytes()
    with store.open_snapshot(db) as conn:
        assert len(store.load_collected(conn).failed) == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM failed")
    assert db.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["s.sqlite3"]


@pytest.mark.parametrize(
    ("setup", "match"),
    [
        ("missing", "no snapshot at .*run `stable-backlog sync` first"),
        ("not-sqlite", "not a stable-backlog snapshot"),
        ("empty-sqlite", "not a stable-backlog snapshot"),
        ("old-version", "schema version 1.*stable-backlog sync"),
    ],
    ids=["missing", "not-sqlite", "empty-sqlite", "old-version"],
)
def test_open_snapshot_refuses_what_is_not_a_current_snapshot(
    tmp_path: Path, setup: str, match: str
) -> None:
    db = tmp_path / "s.sqlite3"
    if setup == "not-sqlite":
        db.write_text("hello\n" * 100)
    elif setup == "empty-sqlite":
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE x (y)")
    elif setup == "old-version":
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    with pytest.raises(store.StoreError, match=match), store.open_snapshot(db):
        pass


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
