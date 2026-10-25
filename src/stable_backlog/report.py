# SPDX-License-Identifier: GPL-2.0-or-later
"""Select, group and label item states for the list and show commands.

Pure functions: nothing here reads files, runs git or prints. Lines meant
for a terminal (table(), show_lines()) are cleaned with sanitize.for_terminal;
every other string still holds text from untrusted messages.

An open request is "no backport found", never "nobody is working on it": the
archive can only show what was mailed.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from stable_backlog.mirror import Collected
from stable_backlog.model import Confidence, Status
from stable_backlog.queue import QueueScan
from stable_backlog.sanitize import for_terminal
from stable_backlog.status import ItemState, version_key
from stable_backlog.store import SnapshotInfo

LORE_LIST_URL = "https://lore.kernel.org/stable/"
# RFC 3986 path characters, left as they are, as public-inbox writes its links;
# "/", "?", "#", "%" and everything else are percent-encoded.
_MESSAGE_ID_SAFE = "@!$&'()*+,;=:._~-"
HASH_QUERY_RE = re.compile(r"[0-9a-f]{12,40}")
SINCE_DAYS_RE = re.compile(r"(?P<days>\d{1,4})d")
SINCE_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

LABELS = {
    Status.FAILED: "no backport found",
    Status.BACKPORT_POSTED: "backport posted",
    Status.QUEUED: "queued",
    Status.RELEASED: "released",
}
TITLE_ONLY = "matched by title only"
EXPORT_FORMAT = "stable-backlog/1"  # change on any incompatible change to the JSON
EXPORT_SKIPPED_SAMPLES = 100


def lore_url(message_id: str) -> str | None:
    """The lore page of a message; None for an id without "@".

    Every RFC 5322 Message-ID has an "@"; without one, an id such as ".."
    would be a dot segment and lead off the stable list.
    """
    if "@" not in message_id:
        return None
    return f"{LORE_LIST_URL}{urllib.parse.quote(message_id, safe=_MESSAGE_ID_SAFE)}/"


def label(state: ItemState) -> str:
    if state.status is Status.RELEASED:
        return f"released in {state.release}"
    if state.status is Status.QUEUED:
        directories = sorted({e.directory for e in state.queue})
        return f"queued ({', '.join(directories)})"
    if state.status is Status.BACKPORT_POSTED and state.confidence is Confidence.TITLE:
        return f"{LABELS[state.status]} ({TITLE_ONLY})"
    return LABELS[state.status]


def first_failed(state: ItemState) -> datetime | None:
    """Date of the first FAILED report of an item (None if none is dated)."""
    dates = [r.envelope.date for r in state.failed if r.envelope.date is not None]
    return min(dates) if dates else None


def author(state: ItemState) -> tuple[str, str]:
    """(name, address) of the commit's author; ("", "") if not found."""
    for r in state.failed:
        if r.author_email:
            return r.author_name, r.author_email
    return "", ""


def files(state: ItemState) -> tuple[str, ...]:
    out: list[str] = []
    for r in state.failed:
        out += [f for f in r.files if f not in out]
    return tuple(out)


# --- selecting --------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    statuses: frozenset[Status] = frozenset({Status.FAILED})
    trees: frozenset[str] = frozenset()  # empty: every tree
    since: datetime | None = None  # first FAILED report on or after this
    author: str | None = None  # part of "name <address>", any case
    path: str | None = None  # a changed file, or a directory holding one

    def __post_init__(self) -> None:
        if self.path is not None and not self.path.strip().strip("/"):
            raise ValueError("an empty path matches nothing; give a directory or a file")


@dataclass(frozen=True)
class Selection:
    items: tuple[ItemState, ...]
    # Left out because a filter could not be checked (the CLI reports these):
    no_date: int = 0  # --since: no dated FAILED report
    no_author: int = 0  # --author: no author found
    no_files: int = 0  # --path: no changed files found


def _path_matches(want: str, paths: Iterable[str]) -> bool:
    return any(p == want or p.startswith(f"{want}/") for p in paths)


def select(states: Iterable[ItemState], filters: Filters) -> Selection:
    """The items that pass every filter, in the order given."""
    items: list[ItemState] = []
    unchecked = {"date": 0, "author": 0, "files": 0}
    path = filters.path.strip().strip("/") if filters.path is not None else None
    needle = filters.author.casefold() if filters.author is not None else None
    for s in states:
        if s.status not in filters.statuses or (filters.trees and s.tree not in filters.trees):
            continue
        if filters.since is not None:
            when = first_failed(s)
            if when is None:
                unchecked["date"] += 1
                continue
            if when < filters.since:
                continue
        if needle is not None:
            name, addr = author(s)
            if not addr:
                unchecked["author"] += 1
                continue
            if needle not in f"{name} <{addr}>".casefold():
                continue
        if path is not None:
            changed = files(s)
            if not changed:
                unchecked["files"] += 1
                continue
            if not _path_matches(path, changed):
                continue
        items.append(s)
    return Selection(tuple(items), unchecked["date"], unchecked["author"], unchecked["files"])


# --- list: one row per commit -----------------------------------------------------------


@dataclass(frozen=True)
class Row:
    upstream: str
    title: str
    trees: tuple[str, ...]  # in version order
    first_failed: datetime | None  # earliest over these trees
    replies: int  # other messages in the FAILED threads


def rows(items: Iterable[ItemState]) -> tuple[Row, ...]:
    """Group items by commit; the longest-waiting first, undated last."""
    by_commit: dict[str, list[ItemState]] = defaultdict(list)
    for s in items:
        by_commit[s.upstream].append(s)
    out = []
    for upstream, group in by_commit.items():
        group.sort(key=lambda s: version_key(s.tree))
        dates = [d for d in map(first_failed, group) if d is not None]
        out.append(
            Row(
                upstream,
                group[0].failed[0].title,
                tuple(s.tree for s in group),
                min(dates) if dates else None,
                sum(len(s.replies) for s in group),
            )
        )
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    out.sort(key=lambda r: (r.first_failed or far_future, r.upstream))
    return tuple(out)


def age_days(when: datetime | None, ref: datetime) -> int | None:
    """Whole days from when to ref (0 if when is later); None if unknown."""
    if when is None:
        return None
    return max(0, (ref - when).days)


def table(rows: Iterable[Row], ref: datetime) -> list[str]:
    """The list table, cleaned for the terminal; ages are counted up to ref."""
    head = ("AGE", "UPSTREAM", "TREES", "REPLIES")
    body = []
    titles = []
    for r in rows:
        age = age_days(r.first_failed, ref)
        body.append(
            (
                "?" if age is None else f"{age}d",
                r.upstream[:12],
                " ".join(r.trees),
                str(r.replies) if r.replies else "-",
            )
        )
        titles.append(for_terminal(r.title))
    widths = [max(len(c[i]) for c in [head, *body]) for i in range(len(head))]

    def line(cells: tuple[str, ...], title: str) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)) + "  " + title

    return [line(head, "TITLE")] + [line(c, t) for c, t in zip(body, titles, strict=True)]


def parse_since(text: str, ref: datetime) -> datetime:
    """--since: "<N>d" (N days before ref) or a date "YYYY-MM-DD" (UTC)."""
    m = SINCE_DAYS_RE.fullmatch(text)
    if m is not None:
        return ref - timedelta(days=int(m.group("days")))
    if SINCE_DATE_RE.fullmatch(text):
        try:
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"--since takes <days>d (e.g. 30d) or a date YYYY-MM-DD, not {text!r:.40}")


# --- show -------------------------------------------------------------------------------


def lookup(states: Iterable[ItemState], query: str) -> tuple[str, ...]:
    """Commits a show query names: 12..40 hex digits of the commit (a
    prefix), or the Message-ID of one of its FAILED reports."""
    query = query.strip()
    if HASH_QUERY_RE.fullmatch(query.lower()):
        prefix = query.lower()
        return tuple(sorted({s.upstream for s in states if s.upstream.startswith(prefix)}))
    mid = query.removeprefix("<").removesuffix(">")
    found = {s.upstream for s in states if any(r.envelope.message_id == mid for r in s.failed)}
    return tuple(sorted(found))


def _day(when: datetime | None) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d") if when else "????-??-??"


def _link(message_id: str | None) -> str:
    if message_id is None:
        return "(no Message-ID)"
    url = lore_url(message_id)
    return url if url is not None else f"(no lore link for Message-ID {message_id})"


def show_lines(upstream: str, states: Iterable[ItemState]) -> list[str]:
    """Where one commit stands in each tree, with lore links; cleaned."""
    items = sorted(
        (s for s in states if s.upstream == upstream), key=lambda s: version_key(s.tree)
    )
    if not items:
        raise ValueError(f"no item for {upstream}")
    name, addr = next((a for a in map(author, items) if a[1]), ("", ""))
    changed: list[str] = []
    for s in items:
        changed += [f for f in files(s) if f not in changed]
    lines = [
        f"{upstream} {items[0].failed[0].title}",
        f"Author: {name} <{addr}>" if addr else "Author: unknown",
        f"Files:  {' '.join(changed)}" if changed else "Files:  unknown",
        "",
    ]
    for s in items:
        lines.append(f"{s.tree:<5} {label(s)}")
        for r in s.failed:
            lines.append(
                f"      FAILED    {_day(r.envelope.date)}  {_link(r.envelope.message_id)}"
            )
        for m in s.backports:
            env = m.post.envelope
            note = f"  ({TITLE_ONLY})" if m.confidence is Confidence.TITLE else ""
            sender = env.sender or "?"
            lines.append(
                f"      backport  {_day(env.date)}  {sender}  {_link(env.message_id)}{note}"
            )
        for reply in s.replies:
            lines.append(f"      reply     {_link(reply)}")
        for e in s.queue:
            lines.append(f"      {e.location:<9} {e.path}")
    return [for_terminal(line) for line in lines]


# --- export --json ---------------------------------------------------------------------


def _iso(when: datetime | None) -> str | None:
    return when.astimezone(timezone.utc).isoformat() if when is not None else None


def _url(message_id: str | None) -> str | None:
    return lore_url(message_id) if message_id is not None else None


def _export_item(s: ItemState) -> dict[str, object]:
    name, addr = author(s)
    return {
        "upstream": s.upstream,
        "tree": s.tree,
        "status": s.status.value,
        "label": label(s),
        "confidence": s.confidence.value if s.confidence is not None else None,
        "title": s.failed[0].title,
        "author": {"name": name, "email": addr} if addr else None,
        "files": list(files(s)),
        "first_failed": _iso(first_failed(s)),
        "failed": [
            {
                "message_id": r.envelope.message_id,
                "date": _iso(r.envelope.date),
                "url": _url(r.envelope.message_id),
            }
            for r in s.failed
        ],
        "backports": [
            {
                "message_id": m.post.envelope.message_id,
                "sender": m.post.envelope.sender,
                "date": _iso(m.post.envelope.date),
                "confidence": m.confidence.value,
                "url": _url(m.post.envelope.message_id),
            }
            for m in s.backports
        ],
        "replies": [{"message_id": mid, "url": lore_url(mid)} for mid in s.replies],
        "queue": [
            {
                "location": e.location,
                "directory": e.directory,
                "path": e.path,
                "release": e.release,
            }
            for e in s.queue
        ],
        "release": s.release,
    }


def export_document(
    collected: Collected,
    scan: QueueScan,
    info: SnapshotInfo,
    states: Iterable[ItemState],
    *,
    generated_at: datetime,
    tool: str,
    lore_source: str,
    queue_source: str,
) -> dict[str, object]:
    """Every item, in every status, with where the data came from (see DESIGN)."""
    items = sorted(states, key=lambda s: (s.upstream, version_key(s.tree)))
    return {
        "format": EXPORT_FORMAT,
        "generated_at": _iso(generated_at),
        "tool": tool,
        "snapshot": {
            "synced_at": _iso(info.synced_at),
            "offline": info.offline,
            "lore": {
                "url": lore_source,
                "rev": collected.mirror_rev,
                "newest": _iso(info.lore_newest),
            },
            "stable_queue": {
                "url": queue_source,
                "rev": scan.rev,
                "newest": _iso(info.queue_newest),
            },
        },
        "messages_read": collected.messages_read,
        "skipped": len(collected.skipped),
        "skipped_samples": [
            {"commit": k.commit, "message_id": k.message_id, "reason": k.reason}
            for k in collected.skipped[:EXPORT_SKIPPED_SAMPLES]
        ],
        "counts": {st.value: sum(1 for s in items if s.status is st) for st in Status},
        "items": [_export_item(s) for s in items],
    }


def export_json(doc: dict[str, object]) -> str:
    """The document as printable ASCII: escapes, controls and non-ASCII become \\uXXXX."""
    text = json.dumps(doc, ensure_ascii=True, indent=1)
    # ensure_ascii leaves DEL as it is, and DEL can only occur inside a string.
    return text.replace("\x7f", "\\u007f") + "\n"
