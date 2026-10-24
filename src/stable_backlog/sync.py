# SPDX-License-Identifier: GPL-2.0-or-later
"""Bring the local mirrors up to date and rebuild the snapshot.

The network is used at most once an hour: a sync within an hour of the last
successful one fails before contacting anyone (an offline rebuild does not).
Then, for each source (the lore stable epoch, then stable-queue):

1. clone it on first use (bare and shallow), otherwise fetch the remote
   master into a private ref. A bare clone has no fetch refspec, so a plain
   `git fetch` would only update FETCH_HEAD and leave master where it was;
2. move master only forward: the old master must be an ancestor of what
   was fetched, because both sources only ever grow;
3. check that the newest commit is recent. An old one means the data
   stopped coming in, and that must not be shown as current.

Then the snapshot is rebuilt from scratch into a temporary file that
replaces the old snapshot only when everything worked. Every failure
raises; the previous snapshot is never touched by a failed sync.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from stable_backlog import cache, gitio, mirror, queue, store

log = logging.getLogger(__name__)

LORE_EPOCH = 2  # the current epoch of lore's stable archive (checked 2026-10-24)
LORE_DAYS = 90  # the first clone holds messages imported in the last 90 days
SNAPSHOT = "snapshot.sqlite"
MASTER = "refs/heads/master"
INCOMING = "refs/stable-backlog/incoming"  # where fetch puts the remote master
ALL_MESSAGES = 10_000_000  # read every message in the local mirror
# To keep the load on lore and git.kernel.org low: after a successful sync,
# the next one may use the network only an hour later (offline is not limited).
MIN_FETCH_INTERVAL = timedelta(hours=1)
LAST_FETCH = "last-fetch"  # time of the last successful sync that used the network


def lore_url(epoch: int) -> str:
    return gitio.validate_url(f"https://lore.kernel.org/stable/git/{epoch}.git")


@dataclass(frozen=True)
class Source:
    name: str  # as given to --reclone
    label: str  # for messages
    url: str
    directory: str  # fixed name inside the cache directory
    max_age: timedelta  # newest commit older than this: the data is stale


LORE = Source(
    "lore",
    f"lore stable epoch {LORE_EPOCH}",
    lore_url(LORE_EPOCH),
    f"lore-stable-{LORE_EPOCH}.git",
    timedelta(days=2),  # longest gap seen in 85 days: 2.4 hours
)
QUEUE = Source(
    "queue",
    "stable-queue",
    gitio.validate_url(gitio.STABLE_QUEUE_URL),
    "stable-queue.git",
    timedelta(days=7),  # longest gap seen in 19 days: 32 hours
)
SOURCES = (LORE, QUEUE)


class SyncError(RuntimeError):
    """The mirrors could not be brought up to date; the old snapshot is kept."""


@dataclass(frozen=True)
class MirrorState:
    source: Source
    path: Path
    rev: str
    previous: str | None  # master before this sync; None if this sync cloned it
    newest: datetime  # committer date of rev


@dataclass(frozen=True)
class SyncResult:
    snapshot: Path
    offline: bool
    lore: MirrorState
    stable_queue: MirrorState
    collected: mirror.Collected
    scan: queue.QueueScan


def clone_limit(source: Source, now: datetime, lore_days: int = LORE_DAYS) -> tuple[str, ...]:
    """How much history a first clone fetches."""
    if source.name != LORE.name:
        return ("--depth=1",)  # stable-queue: only the current state is used
    if lore_days < 1:
        raise ValueError(f"lore_days must be at least 1, not {lore_days}")
    since = (now - timedelta(days=lore_days)).date().isoformat()
    return (f"--shallow-since={since}",)


def sync(
    cache_dir: Path,
    *,
    now: datetime,
    offline: bool = False,
    reclone: Collection[str] = (),
    lore_days: int = LORE_DAYS,
) -> SyncResult:
    """Update both mirrors (unless offline) and rebuild the snapshot."""
    unknown = set(reclone) - {s.name for s in SOURCES}
    if unknown:
        raise ValueError(f"unknown source to reclone: {', '.join(sorted(unknown))}")
    if offline and reclone:
        raise ValueError("reclone needs the network; it cannot be combined with offline")
    root = cache.prepare(cache_dir)
    with cache.lock(root):
        if not offline:
            _check_interval(root, now)
        states: list[MirrorState] = []
        for source in SOURCES:
            if offline:
                states.append(_local(root, source))
                continue
            limit = clone_limit(source, now, lore_days)
            state = _update(root, source, limit, reclone=source.name in reclone)
            _check_fresh(state, now)
            if source is LORE:
                _check_no_newer_epoch(root)
            states.append(state)
        lore, stable_queue = states
        collected, scan = _rebuild(root, lore, stable_queue, now=now, offline=offline)
        if not offline:
            _record_fetch(root, now)
    return SyncResult(root / SNAPSHOT, offline, lore, stable_queue, collected, scan)


def _minutes(n: int) -> str:
    return "1 minute" if n == 1 else f"{n} minutes"


def _last_fetch(root: Path, now: datetime) -> datetime | None:
    """When the last successful network sync ran; None if unknown (warned)."""
    path = root / LAST_FETCH
    try:
        text = path.read_bytes().decode("ascii", "replace").strip()
    except FileNotFoundError:
        return None
    try:
        when: datetime | None = datetime.fromisoformat(text)
    except ValueError:
        when = None
    if when is None or when.tzinfo is None:
        log.warning("cannot read the last fetch time in %s (%r); ignoring it", path, text[:40])
        return None
    if when > now:
        log.warning("the last fetch time in %s (%s) is in the future; ignoring it", path, text)
        return None
    return when


def _check_interval(root: Path, now: datetime) -> None:
    last = _last_fetch(root, now)
    if last is None:
        return
    wait = last + MIN_FETCH_INTERVAL - now
    if wait > timedelta(0):
        ago = int((now - last).total_seconds() // 60)
        left = math.ceil(wait.total_seconds() / 60)
        raise SyncError(
            f"the mirrors were fetched {_minutes(ago)} ago; to keep the load on lore and"
            " git.kernel.org low, sync uses the network at most once an hour."
            f" Try again in {_minutes(left)}, or rebuild the snapshot from the local"
            " mirrors with --offline."
        )


def _record_fetch(root: Path, now: datetime) -> None:
    tmp = root / f"{LAST_FETCH}.tmp"
    tmp.write_text(f"{now.isoformat()}\n", encoding="ascii")
    os.replace(tmp, root / LAST_FETCH)


def _rev(repo: Path, ref: str) -> str:
    out = gitio.run(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    return gitio.validate_full_hash(out.decode("ascii", "replace").strip())


def _commit_time(repo: Path, rev: str) -> datetime:
    out = gitio.run(repo, ["log", "-1", "--format=%cI", gitio.validate_full_hash(rev), "--"])
    return datetime.fromisoformat(out.decode("ascii", "replace").strip().replace("Z", "+00:00"))


def _state(source: Source, path: Path, previous: str | None) -> MirrorState:
    rev = _rev(path, MASTER)
    return MirrorState(source, path, rev, previous, _commit_time(path, rev))


def _local(root: Path, source: Source) -> MirrorState:
    path = root / source.directory
    if not path.is_dir():
        raise SyncError(f"no local copy of {source.label} in {root}; run sync without --offline")
    rev = _rev(path, MASTER)
    return MirrorState(source, path, rev, rev, _commit_time(path, rev))


def _update(root: Path, source: Source, limit: tuple[str, ...], *, reclone: bool) -> MirrorState:
    path = root / source.directory
    if reclone or not path.exists():
        _clone(root, source, path, limit)
        return _state(source, path, None)
    old = _rev(path, MASTER)
    try:
        gitio.network_run(
            path,
            ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head"],
            source.url,
            [f"+{MASTER}:{INCOMING}"],
        )
    except gitio.GitError as e:
        raise SyncError(f"cannot fetch {source.label} ({source.url}) into {path}: {e}") from e
    new = _rev(path, INCOMING)
    if new != old:
        if not gitio.is_ancestor(path, old, new):
            raise SyncError(
                f"{source.label}: the remote history was rewritten (local master {old[:12]}"
                f" is not an ancestor of the fetched {new[:12]}); nothing was changed."
                f" Run `stable-backlog sync --reclone {source.name}` to clone it again."
            )
        gitio.run(path, ["update-ref", MASTER, new, old])
    return _state(source, path, old)


def _clone(root: Path, source: Source, path: Path, limit: tuple[str, ...]) -> None:
    """Clone into a temporary directory, then put it in place of path."""
    new = root / f"{source.directory}.new"
    old = root / f"{source.directory}.old"
    for leftover in (new, old):  # from an interrupted sync
        if leftover.exists():
            shutil.rmtree(leftover)
    try:
        gitio.network_run(
            root, ["clone", "--quiet", "--bare", "--no-tags", *limit], source.url, [str(new)]
        )
        _rev(new, MASTER)  # the clone must have a master branch
    except gitio.GitError as e:
        if new.exists():
            shutil.rmtree(new)
        raise SyncError(f"cannot clone {source.label} from {source.url}: {e}") from e
    if path.exists():
        os.rename(path, old)
        os.rename(new, path)
        shutil.rmtree(old)
    else:
        os.rename(new, path)


def _check_fresh(state: MirrorState, now: datetime) -> None:
    age = now - state.newest
    if age > state.source.max_age:
        raise SyncError(
            f"{state.source.label} looks stale: its newest commit ({state.newest.isoformat()})"
            f" is {age.total_seconds() / 86400:.1f} days old, more than the"
            f" {state.source.max_age.days}-day limit, so it is not shown as current."
            f" Check {state.source.url}."
        )


def _check_no_newer_epoch(root: Path) -> None:
    """Fail if lore has started the next epoch: this one would get no new messages."""
    url = lore_url(LORE_EPOCH + 1)
    try:
        gitio.network_run(root, ["ls-remote", "--heads"], url)
    except gitio.GitError as e:
        log.info("lore epoch %d not found (%s)", LORE_EPOCH + 1, e)
        return
    raise SyncError(
        f"lore has a newer epoch ({url}); this version of stable-backlog reads only epoch"
        f" {LORE_EPOCH} and would miss new messages. Please upgrade stable-backlog."
    )


def _rebuild(
    root: Path, lore: MirrorState, stable_queue: MirrorState, *, now: datetime, offline: bool
) -> tuple[mirror.Collected, queue.QueueScan]:
    collected = mirror.collect(lore.path, ALL_MESSAGES)
    if collected.mirror_rev != lore.rev:
        raise SyncError(f"{lore.path}: HEAD is not {MASTER} ({collected.mirror_rev[:12]})")
    trees = sorted({f.tree for f in collected.failed})
    scan = queue.scan(stable_queue.path, stable_queue.rev, trees)
    info = store.SnapshotInfo(now, offline, lore.newest, stable_queue.newest)
    final = root / SNAPSHOT
    tmp = root / f"{SNAPSHOT}.tmp"
    if tmp.exists():
        tmp.unlink()  # from an interrupted sync
    try:
        with store.open_store(tmp) as conn:
            store.save_collected(conn, collected)
            store.save_queue(conn, scan)
            store.save_snapshot_info(conn, info)
        os.replace(tmp, final)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    return collected, scan
