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
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from stable_backlog import __version__, cache, gitio, mirror, store, sync
from stable_backlog.sanitize import for_terminal

PROG = "stable-backlog"
SKIPPED_SAMPLES = 5  # Message-IDs shown for skipped messages

# The command could not do its job (exit 1 with one line); anything else is a
# bug and keeps its traceback. OSError and sqlite3.Error cover a full disk.
_FAILURES = (
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
    p.add_argument(
        "--cache-dir",
        type=Path,
        help="where the mirrors and the snapshot are kept"
        " (default: $XDG_CACHE_HOME/stable-backlog or ~/.cache/stable-backlog)",
    )
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
    return parser


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    """Run the command; return the exit status (usage errors exit with 2)."""
    args = build_parser().parse_args(argv)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_TerminalFormatter(f"{PROG}: %(levelname)s: %(message)s"))
    logger = logging.getLogger("stable_backlog")
    logger.addHandler(handler)
    try:
        return _sync(args, now or datetime.now(timezone.utc))
    except _FAILURES as e:
        print(f"{PROG}: error: {for_terminal(str(e))}", file=sys.stderr)
        return 1
    finally:
        logger.removeHandler(handler)


def _sync(args: argparse.Namespace, now: datetime) -> int:
    cache_dir = args.cache_dir or cache.default_dir(os.environ)
    result = sync.sync(cache_dir, now=now, offline=args.offline, reclone=args.reclone)
    for line in summary(result):
        print(f"{PROG}: {for_terminal(line)}", file=sys.stderr)
    return 0


def _count(n: int, word: str, plural: str | None = None) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {plural or word + 's'}"


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
        samples = [
            f"{s.message_id or 'commit ' + s.commit[:12]} ({s.reason})" for s in col.skipped
        ]
        more = len(samples) - SKIPPED_SAMPLES
        shown = ", ".join(samples[:SKIPPED_SAMPLES]) + (f" and {more} more" if more > 0 else "")
        lines.append(f"skipped {_count(len(col.skipped), 'message')}: {shown}")
    lines.append(f"stable-queue: {_count(result.scan.patches_read, 'patch', 'patches')} read")
    kind = " (offline rebuild)" if result.offline else ""
    lines.append(f"snapshot: {result.snapshot}{kind}")
    return lines
