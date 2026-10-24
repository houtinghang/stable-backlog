# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for sync: updating the two mirrors, checking them, rebuilding the snapshot.

No test touches the network: the `remotes` fixture (tests/conftest.py)
points the fixed https URLs at local repositories through git's own
configuration, so the code under test runs its real commands and URLs.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stable_backlog import cache, gitio, store, sync
from tests.conftest import (
    FIRST,
    INBOX_IMPORTED,
    LORE_URL,
    NEXT_EPOCH_URL,
    QUEUE_COMMITTED,
    QUEUE_URL,
    SECOND,
    InboxFactory,
    QueueFactory,
    Remotes,
    _git,
    commit_env,
)
from tests.mailfactory import GOLDEN

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
LATER = NOW + sync.MIN_FETCH_INTERVAL  # the next sync that may use the network


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _master(repo: Path) -> str:
    return _git(repo, "rev-parse", "refs/heads/master")


def _snapshot_bytes(cache_dir: Path) -> bytes:
    return (cache_dir / sync.SNAPSHOT).read_bytes()


def _leftovers(cache_dir: Path) -> list[str]:
    return sorted(p.name for p in cache_dir.iterdir() if p.name.endswith((".new", ".old", ".tmp")))


# --- the fixed sources -------------------------------------------------------


def test_sources_are_the_fixed_urls() -> None:
    assert sync.LORE.url == LORE_URL
    assert sync.QUEUE.url == QUEUE_URL
    assert sync.lore_url(3) == NEXT_EPOCH_URL


def test_clone_limits() -> None:
    assert sync.clone_limit(sync.LORE, NOW) == ("--shallow-since=2026-07-27",)
    assert sync.clone_limit(sync.LORE, NOW, lore_days=2) == ("--shallow-since=2026-10-23",)
    assert sync.clone_limit(sync.QUEUE, NOW) == ("--depth=1",)
    with pytest.raises(ValueError, match="lore_days"):
        sync.clone_limit(sync.LORE, NOW, lore_days=0)


# --- first sync and incremental sync -----------------------------------------


def test_first_sync_clones_and_builds_the_snapshot(
    lore_remote: Path, queue_remote: Path, cache_dir: Path
) -> None:
    result = sync.sync(cache_dir, now=NOW)

    assert result.snapshot == cache_dir / sync.SNAPSHOT
    assert not result.offline
    assert result.lore.previous is None
    assert result.lore.rev == _head(lore_remote) == _master(cache_dir / sync.LORE.directory)
    assert result.lore.newest == INBOX_IMPORTED
    assert result.stable_queue.rev == _head(queue_remote)
    assert result.stable_queue.newest == QUEUE_COMMITTED
    assert result.collected.messages_read == 1
    assert len(result.collected.failed) == 1
    assert os.stat(cache_dir).st_mode & 0o777 == 0o700
    assert _leftovers(cache_dir) == []

    with store.open_store(result.snapshot) as conn:
        assert store.load_collected(conn) == result.collected
        assert store.load_queue(conn) == result.scan
        info = store.load_snapshot_info(conn)
    assert info == store.SnapshotInfo(NOW, False, INBOX_IMPORTED, QUEUE_COMMITTED)


def test_second_sync_fetches_and_moves_master_forward(
    make_inbox: InboxFactory,
    remotes: Remotes,
    lore_remote: Path,
    queue_remote: Path,
    cache_dir: Path,
) -> None:
    first = sync.sync(cache_dir, now=NOW)
    grown = make_inbox([FIRST, SECOND])
    assert _git(grown, "rev-list", "--max-parents=0", "HEAD") == first.lore.rev
    remotes.point(LORE_URL, grown)

    result = sync.sync(cache_dir, now=LATER)

    assert result.lore.previous == first.lore.rev
    assert result.lore.rev == _head(grown) == _master(cache_dir / sync.LORE.directory)
    assert result.collected.messages_read == 2
    assert len(result.collected.backports) == 1
    assert result.stable_queue.previous == first.stable_queue.rev  # unchanged queue
    with store.open_store(result.snapshot) as conn:
        assert store.load_collected(conn) == result.collected


def test_second_sync_follows_new_stable_queue_commits(
    lore_remote: Path, queue_remote: Path, cache_dir: Path
) -> None:
    first = sync.sync(cache_dir, now=NOW)
    later = QUEUE_COMMITTED + timedelta(hours=1)
    _git(queue_remote, "commit", "--quiet", "--allow-empty", "-m", "next", env=commit_env(later))

    result = sync.sync(cache_dir, now=LATER)

    assert result.stable_queue.previous == first.stable_queue.rev
    assert result.stable_queue.rev == _head(queue_remote)
    assert result.stable_queue.newest == later


# --- failures leave the old snapshot alone -----------------------------------


def test_rewritten_history_fails_and_keeps_everything(
    make_inbox: InboxFactory,
    remotes: Remotes,
    lore_remote: Path,
    queue_remote: Path,
    cache_dir: Path,
) -> None:
    first = sync.sync(cache_dir, now=NOW)
    before = _snapshot_bytes(cache_dir)
    remotes.point(LORE_URL, make_inbox([SECOND]))  # unrelated history

    with pytest.raises(sync.SyncError, match=r"rewritten.*--reclone lore"):
        sync.sync(cache_dir, now=LATER)

    assert _master(cache_dir / sync.LORE.directory) == first.lore.rev
    assert _snapshot_bytes(cache_dir) == before


def test_reclone_replaces_the_mirror(
    make_inbox: InboxFactory,
    remotes: Remotes,
    lore_remote: Path,
    queue_remote: Path,
    cache_dir: Path,
) -> None:
    sync.sync(cache_dir, now=NOW)
    other = make_inbox([SECOND])
    remotes.point(LORE_URL, other)

    result = sync.sync(cache_dir, now=LATER, reclone={"lore"})

    assert result.lore.previous is None
    assert result.lore.rev == _head(other) == _master(cache_dir / sync.LORE.directory)
    assert result.stable_queue.previous is not None  # only lore was cloned again
    assert _leftovers(cache_dir) == []


@pytest.mark.parametrize(
    ("now", "stale"),
    [
        (INBOX_IMPORTED + timedelta(days=2, minutes=1), "lore"),
        (QUEUE_COMMITTED + timedelta(days=7, minutes=1), "lore"),
    ],
    ids=["lore-over-2-days", "both-old"],
)
def test_stale_mirror_fails(
    lore_remote: Path, queue_remote: Path, cache_dir: Path, now: datetime, stale: str
) -> None:
    with pytest.raises(sync.SyncError, match=rf"{stale}.*stale"):
        sync.sync(cache_dir, now=now)
    assert not (cache_dir / sync.SNAPSHOT).exists()


def test_stale_stable_queue_fails(
    make_queue: QueueFactory, remotes: Remotes, lore_remote: Path, cache_dir: Path
) -> None:
    repo, _ = make_queue(GOLDEN / "stable-queue", when=NOW - timedelta(days=7, minutes=1))
    remotes.point(QUEUE_URL, repo)
    with pytest.raises(sync.SyncError, match=r"stable-queue.*stale"):
        sync.sync(cache_dir, now=NOW)
    assert not (cache_dir / sync.SNAPSHOT).exists()


def test_mirror_just_inside_the_limit_is_fresh(
    lore_remote: Path, queue_remote: Path, cache_dir: Path
) -> None:
    sync.sync(cache_dir, now=INBOX_IMPORTED + timedelta(days=2))


def test_fetch_failure_names_the_source_and_keeps_the_snapshot(
    remotes: Remotes, lore_remote: Path, queue_remote: Path, cache_dir: Path, tmp_path: Path
) -> None:
    sync.sync(cache_dir, now=NOW)
    before = _snapshot_bytes(cache_dir)
    remotes.point(LORE_URL, tmp_path / "gone.git")

    with pytest.raises(
        sync.SyncError, match=r"cannot fetch lore.*lore\.kernel\.org/stable/git/2\.git"
    ):
        sync.sync(cache_dir, now=LATER)

    assert _snapshot_bytes(cache_dir) == before


def test_clone_failure_leaves_no_half_clone(
    remotes: Remotes, queue_remote: Path, cache_dir: Path, tmp_path: Path
) -> None:
    remotes.point(LORE_URL, tmp_path / "gone.git")
    with pytest.raises(sync.SyncError, match="cannot clone lore"):
        sync.sync(cache_dir, now=NOW)
    assert not (cache_dir / sync.LORE.directory).exists()
    assert _leftovers(cache_dir) == []


def test_snapshot_failure_keeps_the_old_snapshot(
    lore_remote: Path, queue_remote: Path, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync.sync(cache_dir, now=NOW)
    before = _snapshot_bytes(cache_dir)

    def broken(*args: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "save_queue", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        sync.sync(cache_dir, now=LATER)

    assert _snapshot_bytes(cache_dir) == before
    assert _leftovers(cache_dir) == []


def test_newer_lore_epoch_fails(
    make_inbox: InboxFactory,
    remotes: Remotes,
    lore_remote: Path,
    queue_remote: Path,
    cache_dir: Path,
) -> None:
    remotes.point(NEXT_EPOCH_URL, make_inbox([SECOND]))
    with pytest.raises(sync.SyncError, match=r"newer epoch.*stable/git/3\.git"):
        sync.sync(cache_dir, now=NOW)
    assert not (cache_dir / sync.SNAPSHOT).exists()


def test_a_running_sync_blocks_another(
    lore_remote: Path, queue_remote: Path, cache_dir: Path
) -> None:
    cache.prepare(cache_dir)
    with cache.lock(cache_dir), pytest.raises(cache.CacheError, match="another sync"):
        sync.sync(cache_dir, now=NOW)


# --- the network is used at most once an hour --------------------------------


def _last_fetch(cache_dir: Path) -> str:
    return (cache_dir / sync.LAST_FETCH).read_text()


def test_network_sync_within_an_hour_is_refused_without_contacting_anyone(
    remotes: Remotes, lore_remote: Path, queue_remote: Path, cache_dir: Path, tmp_path: Path
) -> None:
    first = sync.sync(cache_dir, now=NOW)
    assert _last_fetch(cache_dir) == f"{NOW.isoformat()}\n"
    remotes.point(LORE_URL, tmp_path / "gone.git")  # a fetch would fail differently
    too_soon = LATER - timedelta(minutes=1)
    pattern = r"at most once an hour.*in 1 minute.*--offline"

    with pytest.raises(sync.SyncError, match=pattern):
        sync.sync(cache_dir, now=too_soon)
    with pytest.raises(sync.SyncError, match=pattern):
        sync.sync(cache_dir, now=too_soon, reclone={"lore"})

    assert _master(cache_dir / sync.LORE.directory) == first.lore.rev
    assert _last_fetch(cache_dir) == f"{NOW.isoformat()}\n"


def test_network_sync_an_hour_later_is_allowed(
    lore_remote: Path, queue_remote: Path, cache_dir: Path
) -> None:
    sync.sync(cache_dir, now=NOW)
    sync.sync(cache_dir, now=LATER)
    assert _last_fetch(cache_dir) == f"{LATER.isoformat()}\n"


def test_offline_rebuild_is_not_limited_and_is_not_a_fetch(
    lore_remote: Path, queue_remote: Path, cache_dir: Path
) -> None:
    sync.sync(cache_dir, now=NOW)
    sync.sync(cache_dir, now=NOW + timedelta(minutes=1), offline=True)
    assert _last_fetch(cache_dir) == f"{NOW.isoformat()}\n"
    with pytest.raises(sync.SyncError, match="at most once an hour"):
        sync.sync(cache_dir, now=LATER - timedelta(seconds=1))  # still counted from NOW


def test_a_failed_sync_does_not_count(
    remotes: Remotes, lore_remote: Path, queue_remote: Path, cache_dir: Path, tmp_path: Path
) -> None:
    remotes.point(LORE_URL, tmp_path / "gone.git")
    with pytest.raises(sync.SyncError, match="cannot clone"):
        sync.sync(cache_dir, now=NOW)
    assert not (cache_dir / sync.LAST_FETCH).exists()
    remotes.point(LORE_URL, lore_remote)
    sync.sync(cache_dir, now=NOW + timedelta(minutes=1))


@pytest.mark.parametrize(
    ("content", "warning"),
    [
        (f"{(NOW + timedelta(days=1)).isoformat()}\n", "in the future"),
        ("", "cannot read"),
        ("yesterday\n", "cannot read"),
        ("2026-10-25T12:00:00\n", "cannot read"),  # no time zone
    ],
    ids=["future", "empty", "garbage", "naive"],
)
def test_odd_last_fetch_is_ignored_with_a_warning(
    lore_remote: Path,
    queue_remote: Path,
    cache_dir: Path,
    caplog: pytest.LogCaptureFixture,
    content: str,
    warning: str,
) -> None:
    sync.sync(cache_dir, now=NOW)
    (cache_dir / sync.LAST_FETCH).write_text(content)
    retry = NOW + timedelta(minutes=1)
    with caplog.at_level(logging.WARNING, logger="stable_backlog"):
        sync.sync(cache_dir, now=retry)
    assert warning in caplog.text
    assert _last_fetch(cache_dir) == f"{retry.isoformat()}\n"


# --- offline -----------------------------------------------------------------


def test_offline_rebuilds_without_the_network(
    remotes: Remotes,
    lore_remote: Path,
    queue_remote: Path,
    cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = sync.sync(cache_dir, now=NOW)
    remotes.clear()

    def no_network(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("offline sync used the network")

    monkeypatch.setattr(gitio, "network_run", no_network)
    later = NOW + timedelta(days=30)  # freshness is not checked offline

    result = sync.sync(cache_dir, now=later, offline=True)

    assert result.offline
    assert result.lore.rev == result.lore.previous == first.lore.rev
    with store.open_store(result.snapshot) as conn:
        info = store.load_snapshot_info(conn)
    assert info.offline
    assert info.synced_at == later


def test_offline_needs_existing_mirrors(cache_dir: Path) -> None:
    with pytest.raises(sync.SyncError, match="without --offline"):
        sync.sync(cache_dir, now=NOW, offline=True)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"offline": True, "reclone": {"lore"}}, "network"),
        ({"reclone": {"linux"}}, "unknown"),
    ],
    ids=["offline-reclone", "unknown-source"],
)
def test_bad_arguments(cache_dir: Path, kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        sync.sync(cache_dir, now=NOW, **kwargs)  # type: ignore[arg-type]
    assert not cache_dir.exists()  # rejected before touching anything
