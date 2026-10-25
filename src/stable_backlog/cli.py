# SPDX-License-Identifier: GPL-2.0-or-later
"""The stable-backlog command.

stdout carries only the data a command was asked for (tables, JSON);
summaries, warnings and errors go to stderr. Text that can come from the
archive or from git (subjects, Message-IDs, git's stderr) is passed through
sanitize.for_terminal first, log records included.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from stable_backlog import __version__, cache, gitio, mirror, queue, report, status, store, sync
from stable_backlog.model import Status
from stable_backlog.sanitize import for_terminal
from stable_backlog.status import version_key

PROG = "stable-backlog"
SKIPPED_SAMPLES = 5  # Message-IDs shown for skipped messages
STATUS_NAMES = {
    "failed": Status.FAILED,
    "backport-posted": Status.BACKPORT_POSTED,
    "queued": Status.QUEUED,
    "released": Status.RELEASED,
}


class LookupFailure(LookupError):
    """show found no commit, or more than one, for what it was given."""


# The command could not do its job (exit 1 with one line); anything else is a
# bug and keeps its traceback. OSError and sqlite3.Error cover a full disk.
_FAILURES = (
    LookupFailure,
    sync.SyncError,
    cache.CacheError,
    gitio.GitError,
    mirror.MirrorError,
    store.StoreError,
    OSError,
    sqlite3.Error,
)


class _TerminalFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return for_terminal(super().format(record))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="List Linux stable FAILED backport requests with no backport found yet."
        " Not an official kernel.org tool.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    p = commands.add_parser(
        "sync",
        help="update the local mirrors and rebuild the snapshot",
        description="Fetch the lore stable archive and stable-queue (only new data after the"
        " first run), check that they are current, and rebuild the snapshot.",
    )
    _add_cache_dir(p)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline",
        action="store_true",
        help="rebuild the snapshot from the local mirrors without fetching",
    )
    mode.add_argument(
        "--reclone",
        action="append",
        default=[],
        choices=[s.name for s in sync.SOURCES],
        help="clone this source again, e.g. after its history was rewritten (repeatable)",
    )
    p = commands.add_parser(
        "list",
        help="list the requests with no backport found (from the snapshot, no network)",
        description="One row per upstream commit, the longest-waiting first. Ages are counted"
        " up to the newest lore message in the snapshot.",
    )
    _add_cache_dir(p)
    p.add_argument(
        "--tree", action="append", default=[], type=_tree, help="only this tree (repeatable)"
    )
    which = p.add_mutually_exclusive_group()
    which.add_argument(
        "--status",
        type=_statuses,
        help=f"comma-separated: {', '.join(STATUS_NAMES)} (default: failed, i.e. no backport"
        " found)",
    )
    which.add_argument("--all", action="store_true", help="every status")
    p.add_argument(
        "--since",
        type=_since,
        help="only requests first reported in the last N days (Nd) or on or after a date"
        " (YYYY-MM-DD)",
    )
    p.add_argument("--author", help="only commits whose author name or address contains this")
    p.add_argument("--path", type=_path, help="only commits changing this file or directory")
    p = commands.add_parser(
        "show",
        help="show where one commit stands in each tree, with lore links",
        description="Look up a commit by 12 to 40 hex digits of its upstream hash, or by the"
        " Message-ID of one of its FAILED reports.",
    )
    p.add_argument("query", metavar="COMMIT|MESSAGE-ID")
    _add_cache_dir(p)
    return parser


def _add_cache_dir(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--cache-dir",
        type=Path,
        help="where the mirrors and the snapshot are kept"
        " (default: $XDG_CACHE_HOME/stable-backlog or ~/.cache/stable-backlog)",
    )


def _tree(text: str) -> str:
    if not queue.TREE_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"not a stable tree: {text!r:.40} (e.g. 6.1)")
    return text


def _statuses(text: str) -> frozenset[Status]:
    names = text.split(",")
    unknown = [n for n in names if n not in STATUS_NAMES]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown status {unknown[0]!r:.40}; use {', '.join(STATUS_NAMES)}"
        )
    return frozenset(STATUS_NAMES[n] for n in names)


def _since(text: str) -> str:
    try:
        report.parse_since(text, datetime.now(timezone.utc))  # only checks the form here
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e
    return text


def _path(text: str) -> str:
    try:
        report.Filters(path=text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e
    return text


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    """Run the command; return the exit status (usage errors exit with 2)."""
    args = build_parser().parse_args(argv)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_TerminalFormatter(f"{PROG}: %(levelname)s: %(message)s"))
    logger = logging.getLogger("stable_backlog")
    logger.addHandler(handler)
    commands = {"sync": _sync, "list": _list, "show": _show}
    try:
        return commands[args.command](args, now or datetime.now(timezone.utc))
    except _FAILURES as e:
        print(f"{PROG}: error: {for_terminal(str(e))}", file=sys.stderr)
        return 1
    finally:
        logger.removeHandler(handler)


def _note(lines: Iterable[str]) -> None:
    for line in lines:
        print(f"{PROG}: {for_terminal(line)}", file=sys.stderr)


def _sync(args: argparse.Namespace, now: datetime) -> int:
    cache_dir = args.cache_dir or cache.default_dir(os.environ)
    result = sync.sync(cache_dir, now=now, offline=args.offline, reclone=args.reclone)
    _note(summary(result))
    return 0


@dataclass(frozen=True)
class _Snapshot:
    info: store.SnapshotInfo
    collected: mirror.Collected
    states: tuple[status.ItemState, ...]


def _load(args: argparse.Namespace) -> _Snapshot:
    """Read the snapshot (read-only; never creates the cache directory)."""
    cache_dir = args.cache_dir or cache.default_dir(os.environ)
    with store.open_snapshot(cache_dir / sync.SNAPSHOT) as conn:
        collected = store.load_collected(conn)
        scan = store.load_queue(conn)
        info = store.load_snapshot_info(conn)
    return _Snapshot(info, collected, status.compute(collected, scan))


def _utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _snapshot_notes(snap: _Snapshot, now: datetime) -> list[str]:
    """Where the data comes from, what was skipped, and whether it is old."""
    info = snap.info
    kind = " (offline rebuild)" if info.offline else ""
    lines = [
        f"data: lore up to {_utc(info.lore_newest)}, stable-queue {_utc(info.queue_newest)};"
        f" snapshot built {_utc(info.synced_at)}{kind}"
    ]
    skipped = snap.collected.skipped
    if skipped:
        lines.append(
            f"{_count(len(skipped), 'message')} could not be read when the snapshot was built:"
            f" {_skipped_samples(skipped)}"
        )
    for source, newest in ((sync.LORE, info.lore_newest), (sync.QUEUE, info.queue_newest)):
        age = now - newest
        if age > source.max_age:
            what = "lore message" if source is sync.LORE else "stable-queue commit"
            lines.append(
                f"warning: the newest {what} in the snapshot is {age.total_seconds() / 86400:.1f}"
                " days old; run `stable-backlog sync`"
            )
    return lines


def _list(args: argparse.Namespace, now: datetime) -> int:
    snap = _load(args)
    ref = snap.info.lore_newest
    statuses = frozenset(Status) if args.all else args.status or frozenset({Status.FAILED})
    since = report.parse_since(args.since, ref) if args.since else None
    trees = frozenset(args.tree)
    selection = report.select(
        snap.states, report.Filters(statuses, trees, since, args.author, args.path)
    )
    rows = report.rows(selection.items)
    what = ", ".join(report.LABELS[s] for s in Status if s in statuses)
    if trees:
        what += f" in {' '.join(sorted(trees, key=version_key))}"
    if rows:
        for line in report.table(rows, ref):
            print(line)
        n = len(selection.items)
        lines = [f"{_count(n, 'request')} in {_count(len(rows), 'commit')}: {what}"]
    else:
        lines = [f"no requests match ({what})"]
    for flag, n, missing in (
        ("--since", selection.no_date, "no date"),
        ("--author", selection.no_author, "no author"),
        ("--path", selection.no_files, "no changed files"),
    ):
        if n:
            has, was = ("has", "was") if n == 1 else ("have", "were")
            lines.append(f"{flag}: {_count(n, 'request')} {has} {missing} and {was} left out")
    lines += _snapshot_notes(snap, now)
    if rows:
        lines.append(
            "`stable-backlog show <commit>` lists the FAILED reports, backports and replies"
            " with lore links; a request with replies may already be answered there"
        )
    _note(lines)
    return 0


def _show(args: argparse.Namespace, now: datetime) -> int:
    snap = _load(args)
    found = report.lookup(snap.states, args.query)
    if not found:
        dates = [r.envelope.date for r in snap.collected.failed if r.envelope.date]
        span = (
            f"FAILED reports dated {min(dates):%Y-%m-%d} to {max(dates):%Y-%m-%d}"
            if dates
            else "no dated FAILED reports"
        )
        raise LookupFailure(
            f"no FAILED report for {args.query.strip()} in the snapshot ({span}); give 12 to 40"
            " hex digits of the upstream commit, or the Message-ID of a FAILED report"
        )
    if len(found) > 1:
        raise LookupFailure(
            f"{args.query.strip()} matches {len(found)} commits: {', '.join(found)};"
            " give more digits"
        )
    for line in report.show_lines(found[0], snap.states):
        print(line)
    _note(_snapshot_notes(snap, now))
    return 0


def _count(n: int, word: str, plural: str | None = None) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {plural or word + 's'}"


def _skipped_samples(skipped: Sequence[mirror.Skipped]) -> str:
    samples = [f"{s.message_id or 'commit ' + s.commit[:12]} ({s.reason})" for s in skipped]
    more = len(samples) - SKIPPED_SAMPLES
    return ", ".join(samples[:SKIPPED_SAMPLES]) + (f" and {more} more" if more > 0 else "")


def _mirror_line(state: sync.MirrorState, offline: bool) -> str:
    when = state.newest.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if offline:
        what = f"local copy at {state.rev[:12]}"
    elif state.previous is None:
        what = f"cloned at {state.rev[:12]}"
    elif state.previous == state.rev:
        what = f"unchanged at {state.rev[:12]}"
    else:
        what = f"updated {state.previous[:12]}..{state.rev[:12]}"
    return f"{state.source.label}: {what}, newest commit {when}"


def summary(result: sync.SyncResult) -> list[str]:
    """What a sync did, one line each (not yet cleaned for the terminal)."""
    col = result.collected
    read = _count(col.messages_read, "message")
    failed = _count(len(col.failed), "FAILED report")
    lines = [
        _mirror_line(result.lore, result.offline),
        _mirror_line(result.stable_queue, result.offline),
        f"{read} read: {failed}, {_count(len(col.backports), 'backport')}",
    ]
    if col.skipped:
        shown = _skipped_samples(col.skipped)
        lines.append(f"skipped {_count(len(col.skipped), 'message')}: {shown}")
    lines.append(f"stable-queue: {_count(result.scan.patches_read, 'patch', 'patches')} read")
    kind = " (offline rebuild)" if result.offline else ""
    lines.append(f"snapshot: {result.snapshot}{kind}")
    return lines
