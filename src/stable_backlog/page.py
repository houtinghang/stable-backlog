# SPDX-License-Identifier: GPL-2.0-or-later
"""The web page: one static HTML file, written by `export --html`.

It lists the requests with no backport found, for all trees by commit and
for each tree, with links to the FAILED reports and replies on lore and to
the upstream commits on git.kernel.org.

The page has no JavaScript. Its Content-Security-Policy is a <meta> element
(GitHub Pages cannot set headers) that allows nothing but the page's own
style, by hash. Every piece of text from a message goes through _text():
escape sequences and control characters are removed, then it is escaped
for HTML. Titles and names are wrapped in <bdi>, so bidirectional control
characters cannot reorder the rest of a row. Links come only from report
(lore, git.kernel.org), plus two constants: the project and the JSON file
published next to the page. The output is ASCII; other characters are
written as &#NNNN;.
"""

from __future__ import annotations

import base64
import hashlib
import html
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from datetime import datetime, timezone

from stable_backlog import queue, report
from stable_backlog.mirror import Collected, Skipped
from stable_backlog.model import Status
from stable_backlog.queue import QueueScan
from stable_backlog.sanitize import for_terminal
from stable_backlog.status import ItemState, version_key
from stable_backlog.store import SnapshotInfo

PROJECT_URL = "https://github.com/houtinghang/stable-backlog"
JSON_NAME = "stable-backlog.json"  # published next to the page
SKIPPED_SAMPLES = 20
HEADING = "Linux stable: FAILED backport requests with no backport found"
CSS = (
    ":root{color-scheme:light dark;--fg:#1b1b1b;--bg:#fff;--muted:#5c5c5c;--line:#d9d9d9;"
    "--note:#fff4d6;--note-line:#c99700;--link:#0b57d0}"
    "@media (prefers-color-scheme:dark){:root{--fg:#e6e6e6;--bg:#16181c;--muted:#a3a3a3;"
    "--line:#3a3d42;--note:#33290a;--note-line:#b38600;--link:#8ab4f8}}"
    "body{margin:0 auto;max-width:76rem;padding:1rem;font:15px/1.5 system-ui,sans-serif;"
    "color:var(--fg);background:var(--bg)}"
    "a{color:var(--link)}"
    "h1{font-size:1.4rem;margin:.5rem 0}"
    "h2{font-size:1.15rem;margin:2rem 0 .5rem;padding-bottom:.2rem;"
    "border-bottom:1px solid var(--line)}"
    ".note{background:var(--note);border-left:4px solid var(--note-line);padding:.5rem .75rem}"
    ".muted,footer{color:var(--muted)}"
    ".scroll{overflow-x:auto}"
    "table{border-collapse:collapse;width:100%}"
    "th,td{text-align:left;vertical-align:top;padding:.3rem .5rem;"
    "border-bottom:1px solid var(--line)}"
    ".n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}"
    "code{font:13px ui-monospace,monospace}"
    "nav a{margin-right:.8rem}"
    "footer{margin-top:2.5rem;font-size:.9rem}"
)
NOTICE = (
    "<strong>Not an official kernel.org or stable team tool.</strong>"
    " Early development: these results have not been checked by hand yet, and the error"
    " rate is not known. &ldquo;No backport found&rdquo; means that no backport was found in"
    " the lore stable archive or in stable-queue, not that nobody is working on it. Read the"
    " lore thread before you start a backport."
)


def csp() -> str:
    """The Content-Security-Policy: nothing but the page's own style."""
    digest = base64.b64encode(hashlib.sha256(CSS.encode("ascii")).digest()).decode("ascii")
    return f"default-src 'none'; style-src 'sha256-{digest}'; base-uri 'none'; form-action 'none'"


def render(
    collected: Collected,
    scan: QueueScan,
    info: SnapshotInfo,
    states: Iterable[ItemState],
    *,
    generated_at: datetime,
    tool: str,
) -> str:
    """The whole page, as ASCII text."""
    states = tuple(states)
    trees = sorted({_checked_tree(s.tree) for s in states}, key=version_key)
    open_items = [s for s in states if s.status is Status.FAILED]
    ref = info.lore_newest
    parts = [
        _head(),
        "<body>",
        f"<h1>{HEADING}</h1>",
        f'<p class="note">{NOTICE}</p>',
        *_skipped_note(collected.skipped),
        _data(collected, scan, info, generated_at),
        _nav(trees),
        *_summary(states, trees),
        *_all_trees(open_items, ref),
    ]
    for tree in trees:
        parts += _tree_section(tree, [s for s in open_items if s.tree == tree], ref)
    parts += [*_footer(collected.skipped, tool), "</body>", "</html>"]
    return ("\n".join(parts) + "\n").encode("ascii", "xmlcharrefreplace").decode("ascii")


# --- building blocks ---------------------------------------------------------------


def _text(value: str) -> str:
    """Untrusted text as HTML: escape sequences and controls removed, then escaped."""
    return html.escape(for_terminal(value), quote=True)


def _isolated(value: str) -> str:
    return f"<bdi>{_text(value)}</bdi>"


def _link(url: str | None, label: str) -> str:
    """label (HTML) linked to url; just label without a URL."""
    if url is None:
        return label
    return f'<a href="{html.escape(url, quote=True)}">{label}</a>'


def _lore(message_id: str | None, label: str) -> str:
    return _link(report.lore_url(message_id) if message_id is not None else None, label)


def _commit(upstream: str) -> str:
    url = report.commit_url(upstream)  # checks that it is a full commit id
    return _link(url, f"<code>{upstream[:12]}</code>")


def _checked_tree(tree: str) -> str:
    """A tree name is used in id="tree-X.Y"; only "X.Y" is accepted."""
    if not queue.TREE_RE.fullmatch(tree):
        raise ValueError(f"not a stable tree: {tree!r:.40}")
    return tree


def _tree_id(tree: str) -> str:
    return f"tree-{_checked_tree(tree)}"


def _utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _day(when: datetime | None) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d") if when else "date unknown"


def _age(when: datetime | None, ref: datetime) -> str:
    days = report.age_days(when, ref)
    return "?" if days is None else f"{days}d"


def _count(n: int, word: str, plural: str | None = None) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {plural or word + 's'}"


def _table(
    head: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    numbers: Collection[int] = (0,),
    row_header: bool = False,
) -> list[str]:
    """A table of HTML cells. Columns in `numbers` are right-aligned; with
    row_header, the first cell of each row is a row header."""

    def cls(i: int) -> str:
        return ' class="n"' if i in numbers else ""

    def cell(i: int, c: str) -> str:
        if i == 0 and row_header:
            return f'<th scope="row"{cls(i)}>{c}</th>'
        return f"<td{cls(i)}>{c}</td>"

    cells = "".join(f'<th scope="col"{cls(i)}>{h}</th>' for i, h in enumerate(head))
    out = ['<div class="scroll"><table>', f"<thead><tr>{cells}</tr></thead>", "<tbody>"]
    out += [f"<tr>{''.join(cell(i, c) for i, c in enumerate(row))}</tr>" for row in rows]
    return [*out, "</tbody></table></div>"]


# --- sections ------------------------------------------------------------------------


def _head() -> str:
    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            f'<meta http-equiv="Content-Security-Policy" content="{csp()}">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{HEADING} (stable-backlog)</title>",
            f"<style>{CSS}</style>",
            "</head>",
        ]
    )


def _skipped_note(skipped: Sequence[Skipped]) -> list[str]:
    if not skipped:
        return []
    return [
        f'<p class="note">{_count(len(skipped), "message")} could not be read when the data'
        " was built; a request answered only in such a message may be listed here by mistake."
        " They are listed at the end of the page.</p>"
    ]


def _data(collected: Collected, scan: QueueScan, info: SnapshotInfo, built: datetime) -> str:
    dates = [r.envelope.date for r in collected.failed if r.envelope.date is not None]
    covered = (
        f"FAILED reports from {_day(min(dates))} to {_day(max(dates))}."
        if dates
        else "There are no dated FAILED reports."
    )
    offline = (
        " The data was rebuilt from the local mirrors without fetching." if info.offline else ""
    )
    queue_rev = _link(report.queue_commit_url(scan.rev), f"<code>{scan.rev[:12]}</code>")
    return (
        f'<p class="muted">Data: lore stable archive up to {_utc(info.lore_newest)};'
        f" stable-queue {queue_rev} ({_utc(info.queue_newest)}). {covered}{offline}"
        f" Page built {_utc(built)}. The published page is rebuilt once a day: if this date is"
        " more than two days old, its update is failing.</p>"
    )


def _nav(trees: Sequence[str]) -> str:
    links = ['<a href="#all">all trees</a>']
    links += [f'<a href="#{_tree_id(t)}">{_text(t)}</a>' for t in trees]
    return f"<nav>Jump to: {' '.join(links)}</nav>"


def _summary(states: Sequence[ItemState], trees: Sequence[str]) -> list[str]:
    counts = Counter((s.tree, s.status) for s in states)
    head = ["Tree", *(report.LABELS[st].capitalize() for st in Status)]
    rows = [
        [f'<a href="#{_tree_id(t)}">{_text(t)}</a>', *(f"{counts[t, st]:,}" for st in Status)]
        for t in trees
    ]
    totals = Counter(s.status for s in states)
    rows.append(["All", *(f"{totals[st]:,}" for st in Status)])
    return [
        '<h2 id="summary">Requests by tree</h2>',
        *_table(head, rows, numbers=range(1, len(head)), row_header=True),
        '<p class="muted">A request is one (upstream commit, stable tree) pair with a FAILED'
        " report. Backport posted: someone mailed a backport for that tree. Queued: in"
        " stable-queue <code>queue-X.Y</code> or <code>staging-X.Y</code>. Released: in"
        " <code>releases/</code>.</p>",
    ]


def _all_trees(items: Sequence[ItemState], ref: datetime) -> list[str]:
    rows = report.rows(items)
    out = [f'<h2 id="all">All trees: {_count(len(rows), "commit")} with no backport found</h2>']
    if not rows:
        return [*out, "<p>None.</p>"]
    by_key = {(s.upstream, s.tree): s for s in items}
    replied = sum(1 for s in items if s.replies)
    note = (
        f" {_count(replied, 'request')} {'has' if replied == 1 else 'have'} replies in the"
        " FAILED thread and may already be answered: read them before you start a backport."
        if replied
        else ""
    )
    out.append(
        '<p class="muted">The longest-waiting first. Age: days from the first FAILED report'
        " to the newest message in the archive. Each tree links to its FAILED report."
        f"{note}</p>"
    )
    body = []
    for r in rows:
        group = [by_key[r.upstream, t] for t in r.trees]
        trees = " ".join(_lore(s.failed[0].envelope.message_id, _text(s.tree)) for s in group)
        name = next((n for n, a in map(report.author, group) if a), "")
        replies = _count(r.replies, "reply", "replies") if r.replies else ""
        age = _age(r.first_failed, ref)
        body.append([age, _commit(r.upstream), _isolated(r.title), _name(name), trees, replies])
    head = ["Age", "Commit", "Title", "Author", "Trees", "Replies"]
    return [*out, *_table(head, body)]


def _name(name: str) -> str:
    return _isolated(name) if name else ""


def _tree_section(tree: str, items: Sequence[ItemState], ref: datetime) -> list[str]:
    out = [
        f'<h2 id="{_tree_id(tree)}">{_text(tree)}: {_count(len(items), "request")}'
        " with no backport found</h2>"
    ]
    if not items:
        return [*out, "<p>None.</p>"]
    by_upstream = {s.upstream: s for s in items}
    body = []
    for r in report.rows(items):
        s = by_upstream[r.upstream]
        reports = " ".join(_lore(f.envelope.message_id, _day(f.envelope.date)) for f in s.failed)
        replies = " ".join(_lore(m, f"reply {i}") for i, m in enumerate(s.replies, 1))
        name = _name(report.author(s)[0])
        age = _age(r.first_failed, ref)
        body.append([age, _commit(s.upstream), _isolated(r.title), name, reports, replies])
    head = ["Age", "Commit", "Title", "Author", "FAILED report", "Replies"]
    return [*out, *_table(head, body)]


def _skipped_sample(k: Skipped) -> str:
    if k.message_id is None:
        what = f"commit <code>{_text(k.commit[:12])}</code>"
    else:
        what = _lore(k.message_id, _text(k.message_id))
    return f"{what} ({_text(k.reason)})"


def _footer(skipped: Sequence[Skipped], tool: str) -> list[str]:
    out = ["<footer>"]
    if skipped:
        samples = ", ".join(_skipped_sample(k) for k in skipped[:SKIPPED_SAMPLES])
        more = len(skipped) - SKIPPED_SAMPLES
        tail = f" and {more:,} more" if more > 0 else ""
        out.append(
            f"<p>{_count(len(skipped), 'message')} could not be read when the data was built:"
            f" {samples}{tail}.</p>"
        )
    out += [
        "<p>Every request in every status, with the addresses of authors and senders and the"
        f" stable-queue files, is also published as JSON: {_link(JSON_NAME, JSON_NAME)}"
        " (its format is described in docs/DESIGN.md in the project).</p>",
        f"<p>Made by {_text(tool)} from the public lore stable archive and stable-queue."
        " Source code, how AI assistance was used, and where to report a wrong entry:"
        f" {_link(PROJECT_URL, 'github.com/houtinghang/stable-backlog')}.</p>",
        "</footer>",
    ]
    return out
