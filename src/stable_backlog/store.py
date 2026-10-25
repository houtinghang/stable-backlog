# SPDX-License-Identifier: GPL-2.0-or-later
"""SQLite persistence for parsed records and the stable-queue scan.

A store holds one snapshot: saving replaces what was there. Every statement
uses ? parameters; no value is ever formatted into SQL text.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from stable_backlog.mirror import Collected, Skipped
from stable_backlog.model import (
    BackportPost,
    Envelope,
    FailedRequest,
    Location,
    OtherMail,
    QueueEntry,
    ReviewMail,
)
from stable_backlog.queue import QueueScan

SCHEMA_VERSION = "2"

# All SQL is fixed text; values only ever go in through ? parameters.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS failed (
    seq INTEGER PRIMARY KEY,
    message_id TEXT, subject TEXT, sender TEXT, date TEXT, in_reply_to TEXT, refs TEXT,
    upstream TEXT NOT NULL, tree TEXT NOT NULL, title TEXT NOT NULL,
    author_name TEXT NOT NULL, author_email TEXT NOT NULL, files TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS backport (
    seq INTEGER PRIMARY KEY,
    message_id TEXT, subject TEXT, sender TEXT, date TEXT, in_reply_to TEXT, refs TEXT,
    trees TEXT NOT NULL, upstreams TEXT NOT NULL, title TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review (
    seq INTEGER PRIMARY KEY,
    message_id TEXT, subject TEXT, sender TEXT, date TEXT, in_reply_to TEXT, refs TEXT,
    tree TEXT NOT NULL, upstreams TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS other (
    seq INTEGER PRIMARY KEY,
    message_id TEXT, subject TEXT, sender TEXT, date TEXT, in_reply_to TEXT, refs TEXT,
    note TEXT);
CREATE TABLE IF NOT EXISTS skipped (
    seq INTEGER PRIMARY KEY, commit_id TEXT NOT NULL, message_id TEXT, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS removed (seq INTEGER PRIMARY KEY, message_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS queue_entry (
    seq INTEGER PRIMARY KEY, upstream TEXT NOT NULL, tree TEXT NOT NULL,
    location TEXT NOT NULL, directory TEXT NOT NULL, path TEXT NOT NULL, release TEXT);
"""
_DELETE_MESSAGES = (
    "DELETE FROM failed",
    "DELETE FROM backport",
    "DELETE FROM review",
    "DELETE FROM other",
    "DELETE FROM skipped",
    "DELETE FROM removed",
)
_INSERT_FAILED = (
    "INSERT INTO failed (seq, message_id, subject, sender, date, in_reply_to, refs,"
    " upstream, tree, title, author_name, author_email, files)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_INSERT_BACKPORT = (
    "INSERT INTO backport (seq, message_id, subject, sender, date, in_reply_to, refs,"
    " trees, upstreams, title) VALUES (?,?,?,?,?,?,?,?,?,?)"
)
_INSERT_REVIEW = (
    "INSERT INTO review (seq, message_id, subject, sender, date, in_reply_to, refs,"
    " tree, upstreams) VALUES (?,?,?,?,?,?,?,?,?)"
)
_INSERT_OTHER = (
    "INSERT INTO other (seq, message_id, subject, sender, date, in_reply_to, refs,"
    " note) VALUES (?,?,?,?,?,?,?,?)"
)
_SELECT_FAILED = (
    "SELECT message_id, subject, sender, date, in_reply_to, refs, upstream, tree, title,"
    " author_name, author_email, files FROM failed ORDER BY seq"
)
_SELECT_BACKPORT = (
    "SELECT message_id, subject, sender, date, in_reply_to, refs, trees, upstreams, title"
    " FROM backport ORDER BY seq"
)
_SELECT_REVIEW = (
    "SELECT message_id, subject, sender, date, in_reply_to, refs, tree, upstreams"
    " FROM review ORDER BY seq"
)
_SELECT_OTHER = (
    "SELECT message_id, subject, sender, date, in_reply_to, refs, note FROM other ORDER BY seq"
)


class StoreError(RuntimeError):
    """The store is missing data or was written by an incompatible version."""


@dataclass(frozen=True)
class SnapshotInfo:
    """When and from what a snapshot was built."""

    synced_at: datetime
    offline: bool  # rebuilt from the local mirrors without fetching
    lore_newest: datetime  # committer (import) date of the newest lore commit
    queue_newest: datetime  # committer date of the stable-queue commit


def save_snapshot_info(conn: sqlite3.Connection, info: SnapshotInfo) -> None:
    _set_meta(
        conn,
        {
            "synced_at": info.synced_at.isoformat(),
            "offline": int(info.offline),
            "lore_newest": info.lore_newest.isoformat(),
            "queue_newest": info.queue_newest.isoformat(),
        },
    )


def load_snapshot_info(conn: sqlite3.Connection) -> SnapshotInfo:
    meta = _meta(conn)
    if "synced_at" not in meta:
        raise StoreError("no sync information in the store (run sync first)")
    return SnapshotInfo(
        datetime.fromisoformat(meta["synced_at"]),
        meta["offline"] == "1",
        datetime.fromisoformat(meta["lore_newest"]),
        datetime.fromisoformat(meta["queue_newest"]),
    )


@contextmanager
def open_store(path: Path) -> Iterator[sqlite3.Connection]:
    """Open (creating if needed) a store; commit on success, roll back on error."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        version = _meta(conn).get("schema_version")
        if version is not None:
            _check_version(path, version)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def open_snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a snapshot read-only: nothing is created or written, not even a
    journal. sync replaces the file atomically, so it is always complete."""
    if not path.is_file():
        raise StoreError(f"no snapshot at {path}; run `stable-backlog sync` first")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        try:
            version = _meta(conn).get("schema_version")
        except sqlite3.DatabaseError as e:
            raise StoreError(f"{path}: not a stable-backlog snapshot ({e})") from e
        _check_version(path, version)
        yield conn
    finally:
        conn.close()


def _check_version(path: Path, version: str | None) -> None:
    if version != SCHEMA_VERSION:
        raise StoreError(
            f"{path}: schema version {version}, expected {SCHEMA_VERSION}; rebuild it with"
            " `stable-backlog sync` (--offline is enough)"
        )


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM meta").fetchall())


def _set_meta(conn: sqlite3.Connection, values: dict[str, str | int]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        [(k, str(v)) for k, v in values.items()],
    )


def _env_row(env: Envelope) -> tuple[Any, ...]:
    date = env.date.isoformat() if env.date else None
    refs = json.dumps(list(env.references))
    return (env.message_id, env.subject, env.sender, date, env.in_reply_to, refs)


def _env(row: Sequence[Any]) -> Envelope:
    mid, subject, sender, date, in_reply_to, refs = row
    when = datetime.fromisoformat(date) if date else None
    return Envelope(mid, subject, sender, when, in_reply_to, tuple(json.loads(refs)))


def _list(value: str) -> tuple[str, ...]:
    return tuple(json.loads(value))


def save_collected(conn: sqlite3.Connection, collected: Collected) -> None:
    for statement in _DELETE_MESSAGES:
        conn.execute(statement)
    conn.executemany(
        _INSERT_FAILED,
        [
            (
                i,
                *_env_row(r.envelope),
                r.upstream,
                r.tree,
                r.title,
                r.author_name,
                r.author_email,
                json.dumps(list(r.files)),
            )
            for i, r in enumerate(collected.failed)
        ],
    )
    conn.executemany(
        _INSERT_BACKPORT,
        [
            (
                i,
                *_env_row(r.envelope),
                json.dumps(list(r.trees)),
                json.dumps(list(r.upstreams)),
                r.title,
            )
            for i, r in enumerate(collected.backports)
        ],
    )
    conn.executemany(
        _INSERT_REVIEW,
        [
            (i, *_env_row(r.envelope), r.tree, json.dumps(list(r.upstreams)))
            for i, r in enumerate(collected.reviews)
        ],
    )
    conn.executemany(
        _INSERT_OTHER, [(i, *_env_row(r.envelope), r.note) for i, r in enumerate(collected.others)]
    )
    conn.executemany(
        "INSERT INTO skipped (seq, commit_id, message_id, reason) VALUES (?,?,?,?)",
        [(i, s.commit, s.message_id, s.reason) for i, s in enumerate(collected.skipped)],
    )
    conn.executemany(
        "INSERT INTO removed (seq, message_id) VALUES (?,?)", list(enumerate(collected.removed))
    )
    _set_meta(conn, {"mirror_rev": collected.mirror_rev, "messages_read": collected.messages_read})


def load_collected(conn: sqlite3.Connection) -> Collected:
    meta = _meta(conn)
    if "mirror_rev" not in meta:
        raise StoreError("no mirror data in the store (run sync first)")
    failed = tuple(
        FailedRequest(_env(r[:6]), r[6], r[7], r[8], r[9], r[10], _list(r[11]))
        for r in conn.execute(_SELECT_FAILED)
    )
    backports = tuple(
        BackportPost(_env(r[:6]), _list(r[6]), _list(r[7]), r[8])
        for r in conn.execute(_SELECT_BACKPORT)
    )
    reviews = tuple(
        ReviewMail(_env(r[:6]), r[6], _list(r[7])) for r in conn.execute(_SELECT_REVIEW)
    )
    others = tuple(OtherMail(_env(r[:6]), r[6]) for r in conn.execute(_SELECT_OTHER))
    skipped = tuple(
        Skipped(*r)
        for r in conn.execute("SELECT commit_id, message_id, reason FROM skipped ORDER BY seq")
    )
    removed = tuple(r[0] for r in conn.execute("SELECT message_id FROM removed ORDER BY seq"))
    return Collected(
        meta["mirror_rev"],
        int(meta["messages_read"]),
        failed,
        backports,
        reviews,
        others,
        skipped,
        removed,
    )


def save_queue(conn: sqlite3.Connection, scan: QueueScan) -> None:
    conn.execute("DELETE FROM queue_entry")
    conn.executemany(
        "INSERT INTO queue_entry (seq, upstream, tree, location, directory, path, release)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            (i, e.upstream, e.tree, e.location, e.directory, e.path, e.release)
            for i, e in enumerate(scan.entries)
        ],
    )
    _set_meta(
        conn,
        {
            "queue_rev": scan.rev,
            "queue_patches_read": scan.patches_read,
            "queue_without_marker": scan.without_marker,
            "queue_not_in_series": scan.not_in_series,
        },
    )


def load_queue(conn: sqlite3.Connection) -> QueueScan:
    meta = _meta(conn)
    if "queue_rev" not in meta:
        raise StoreError("no stable-queue data in the store (run sync first)")
    entries = tuple(
        QueueEntry(r[0], r[1], cast(Location, r[2]), r[3], r[4], r[5])
        for r in conn.execute(
            "SELECT upstream, tree, location, directory, path, release"
            " FROM queue_entry ORDER BY seq"
        )
    )
    return QueueScan(
        meta["queue_rev"],
        entries,
        int(meta["queue_patches_read"]),
        int(meta["queue_without_marker"]),
        int(meta["queue_not_in_series"]),
    )
