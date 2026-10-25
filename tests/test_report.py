# SPDX-License-Identifier: GPL-2.0-or-later
"""Unit tests for report: selecting, grouping and labelling items, links.

States are built from parsed records directly (no git), through
status.compute, so the tests see the same items the commands do.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from stable_backlog import mirror, queue, report, status, store
from stable_backlog.model import OtherMail, Status
from tests.records import REF, backport, compute, entry, env, failed

H1 = "1111111111111111111111111111111111111111"
H2 = "2222222222222222222222222222222222222222"
H3 = "2222222222223333333333333333333333333333"  # same first 12 digits as H2
PREFIX = "https://lore.kernel.org/stable/"


# --- lore links ------------------------------------------------------------------


def test_lore_url_of_a_failed_report() -> None:
    assert (
        report.lore_url("2026100550-nest-aviator-8a3f@gregkh")
        == PREFIX + "2026100550-nest-aviator-8a3f@gregkh/"
    )


@pytest.mark.parametrize(
    "mid",
    [
        "../x@y",
        "x/../../y@z",
        "a?b=c@d",
        "a#frag@d",
        "%2e%2e@x",
        "..%2f..@x",
        "a\\b@c",
        "<a@b>",
        "a'\"<>@b",
        "a@b/c",
    ],
    ids=[
        "dotdot",
        "nested-dotdot",
        "query",
        "fragment",
        "encoded-dots",
        "encoded-slash",
        "backslash",
        "angle",
        "quotes",
        "slash-in-domain",
    ],
)
def test_lore_url_stays_on_the_message_page(mid: str) -> None:
    url = report.lore_url(mid)
    assert url is not None
    assert url.startswith(PREFIX)
    assert url.endswith("/")
    segment = url[len(PREFIX) : -1]
    # Only RFC 3986 path characters and %XX: no / ? # \ " < > or spaces.
    assert re.fullmatch(r"(?:[A-Za-z0-9@!$&'()*+,;=:._~-]|%[0-9A-F]{2})+", segment)
    assert segment not in (".", "..")
    assert urllib.parse.unquote(segment) == mid
    parts = urllib.parse.urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.query, parts.fragment) == (
        "https",
        "lore.kernel.org",
        "",
        "",
    )
    assert parts.path.count("/") == 3  # /stable/<one segment>/


@pytest.mark.parametrize(
    "mid", ["..", ".", "no-at-sign", ""], ids=["dotdot", "dot", "plain", "empty"]
)
def test_lore_url_needs_an_at_sign(mid: str) -> None:
    # RFC 5322 Message-IDs have one; without it a "." or ".." could climb up.
    assert report.lore_url(mid) is None


def test_commit_links_go_to_git_kernel_org() -> None:
    assert report.commit_url(H1) == (
        f"https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id={H1}"
    )
    assert report.queue_commit_url(H2) == (
        f"https://git.kernel.org/pub/scm/linux/kernel/git/stable/stable-queue.git/commit/?id={H2}"
    )


@pytest.mark.parametrize(
    "commit",
    [H1[:12], "AB" * 20, f"{H1}&x=1", f"{H1}#f", f"{H1[:39]}\n", "", "../" + H1[3:]],
    ids=["short", "upper-case", "query", "fragment", "newline", "empty", "dotdot"],
)
def test_commit_links_need_a_full_commit_id(commit: str) -> None:
    with pytest.raises(ValueError, match="full commit"):
        report.commit_url(commit)
    with pytest.raises(ValueError, match="full commit"):
        report.queue_commit_url(commit)


# --- labels ----------------------------------------------------------------------


def test_labels_never_claim_more_than_the_archive_shows() -> None:
    states = compute(
        [
            failed(H1, "6.1", "f1@gregkh"),
            failed(H1, "6.6", "f2@gregkh"),
            failed(H2, "6.1", "f3@gregkh", title="bar: baz"),
            failed(H2, "6.6", "f4@gregkh", title="bar: baz"),
            failed(H2, "6.12", "f5@gregkh", title="bar: baz"),
        ],
        backports=[backport("6.6", H1, "b1@x"), backport("6.1", None, "b2@x", title="bar: baz")],
        entries=[entry(H2, "6.6", "queue-6.6"), entry(H2, "6.12", "releases/6.12.40")],
    )
    labels = {(s.upstream, s.tree): report.label(s) for s in states}
    assert labels == {
        (H1, "6.1"): "no backport found",
        (H1, "6.6"): "backport posted",
        (H2, "6.1"): "backport posted (matched by title only)",
        (H2, "6.6"): "queued (queue-6.6)",
        (H2, "6.12"): "released in 6.12.40",
    }
    assert not any("nobody" in label for label in labels.values())


# --- selecting -------------------------------------------------------------------


def keys(selection: report.Selection) -> list[tuple[str, str]]:
    return sorted((s.upstream, s.tree) for s in selection.items)


@pytest.fixture
def mixed() -> tuple[status.ItemState, ...]:
    return compute(
        [
            failed(H1, "6.1", "f1@gregkh", days_ago=40),
            failed(H1, "6.6", "f2@gregkh", days_ago=40),
            failed(H2, "6.1", "f3@gregkh", days_ago=5, files=["drivers/tty/tty_port.c"]),
            failed(
                H3,
                "5.10",
                "f4@gregkh",
                days_ago=20,
                author=("Thomas Hellström", "Thomas.Hellstrom@linux.intel.com"),
                files=["drivers/ttyx.c", "mm/x.c"],
            ),
        ],
        backports=[backport("6.6", H1, "b1@x")],
    )


def test_default_selection_is_no_backport_found(mixed: tuple[status.ItemState, ...]) -> None:
    sel = report.select(mixed, report.Filters())
    assert keys(sel) == [(H1, "6.1"), (H2, "6.1"), (H3, "5.10")]
    assert all(s.status is Status.FAILED for s in sel.items)


def test_select_by_status_and_tree(mixed: tuple[status.ItemState, ...]) -> None:
    posted = report.Filters(statuses=frozenset({Status.BACKPORT_POSTED}))
    assert keys(report.select(mixed, posted)) == [(H1, "6.6")]
    every = report.Filters(statuses=frozenset(Status), trees=frozenset({"6.1", "6.6"}))
    assert keys(report.select(mixed, every)) == [(H1, "6.1"), (H1, "6.6"), (H2, "6.1")]


def test_since_keeps_requests_first_reported_on_or_after_it(
    mixed: tuple[status.ItemState, ...],
) -> None:
    sel = report.select(mixed, report.Filters(since=REF - timedelta(days=20)))
    assert keys(sel) == [(H2, "6.1"), (H3, "5.10")]


@pytest.mark.parametrize(
    ("author", "expected"),
    [
        ("hellström", [(H3, "5.10")]),
        ("thomas.hellstrom@linux.intel.com", [(H3, "5.10")]),
        ("AUTHOR@EXAMPLE.ORG", [(H1, "6.1"), (H2, "6.1")]),
        ("nobody", []),
    ],
    ids=["name", "address-other-case", "address-upper", "no-match"],
)
def test_author_matches_name_or_address_ignoring_case(
    mixed: tuple[status.ItemState, ...], author: str, expected: list[tuple[str, str]]
) -> None:
    assert keys(report.select(mixed, report.Filters(author=author))) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("drivers/tty", [(H2, "6.1")]),
        ("drivers/tty/", [(H2, "6.1")]),
        ("/drivers/tty", [(H2, "6.1")]),
        ("drivers/tty/tty_port.c", [(H2, "6.1")]),
        ("drivers", [(H1, "6.1"), (H2, "6.1"), (H3, "5.10")]),
        ("mm", [(H3, "5.10")]),
        ("drivers/tt", []),
    ],
    ids=[
        "directory",
        "trailing-slash",
        "leading-slash",
        "file",
        "parent",
        "other",
        "partial-name",
    ],
)
def test_path_matches_whole_path_components(
    mixed: tuple[status.ItemState, ...], path: str, expected: list[tuple[str, str]]
) -> None:
    assert keys(report.select(mixed, report.Filters(path=path))) == expected


@pytest.mark.parametrize("path", ["", "/", " "], ids=["empty", "root", "space"])
def test_empty_path_is_refused(path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        report.Filters(path=path)


def test_requests_a_filter_cannot_check_are_left_out_and_counted() -> None:
    # Never silently: the CLI reports these counts.
    states = compute(
        [
            failed(H1, "6.1", "f1@gregkh", days_ago=None, author=("", ""), files=()),
            failed(H2, "6.1", "f2@gregkh"),
        ]
    )
    sel = report.select(states, report.Filters(since=REF - timedelta(days=30)))
    assert (keys(sel), sel.no_date) == ([(H2, "6.1")], 1)
    sel = report.select(states, report.Filters(author="author"))
    assert (keys(sel), sel.no_author) == ([(H2, "6.1")], 1)
    sel = report.select(states, report.Filters(path="drivers"))
    assert (keys(sel), sel.no_files) == ([(H2, "6.1")], 1)
    sel = report.select(states, report.Filters())
    assert (sel.no_date, sel.no_author, sel.no_files) == (0, 0, 0)


# --- rows (one per commit) ----------------------------------------------------------


def test_rows_group_trees_per_commit_oldest_first() -> None:
    states = compute(
        [
            failed(H2, "6.1", "f1@gregkh", days_ago=5, title="newer: one"),
            failed(H1, "6.12", "f2@gregkh", days_ago=30, title="older: two"),
            failed(H1, "5.10", "f3@gregkh", days_ago=31, title="older: two"),
            failed(H1, "6.6", "f4@gregkh", days_ago=29, title="older: two"),
        ],
        others=[
            OtherMail(env("r1@x", reply_to="f2@gregkh")),
            OtherMail(env("r2@x", reply_to="f3@gregkh")),
        ],
    )
    rows = report.rows(report.select(states, report.Filters()).items)
    assert [(r.upstream, r.trees, r.replies, r.title) for r in rows] == [
        (H1, ("5.10", "6.6", "6.12"), 2, "older: two"),
        (H2, ("6.1",), 0, "newer: one"),
    ]
    assert rows[0].first_failed == REF - timedelta(days=31)
    assert report.age_days(rows[0].first_failed, REF) == 31


def test_undated_rows_come_last_with_an_unknown_age() -> None:
    states = compute([failed(H1, "6.1", "f1@gregkh", days_ago=None), failed(H2, "6.1", "f2@x")])
    rows = report.rows(states)
    assert [r.upstream for r in rows] == [H2, H1]
    assert report.age_days(rows[1].first_failed, REF) is None


def test_age_of_a_date_after_the_reference_is_zero() -> None:
    assert report.age_days(REF + timedelta(days=3), REF) == 0


def test_table_columns_and_cleaned_text() -> None:
    states = compute(
        [
            failed(H1, "6.1", "f1@gregkh", days_ago=80, title="mm: \x1b]0;owned\x07fix\x9b it"),
            failed(H1, "5.15", "f2@gregkh", days_ago=79, title="mm: fix it"),
        ],
        others=[OtherMail(env("r1@x", reply_to="f1@gregkh"))],
    )
    lines = report.table(report.rows(states), REF)
    assert lines[0].split() == ["AGE", "UPSTREAM", "TREES", "REPLIES", "TITLE"]
    assert lines[1].split() == ["80d", H1[:12], "5.15", "6.1", "1", "mm:", "fix", "it"]
    assert all("\x1b" not in line and "\x07" not in line and "\x9b" not in line for line in lines)
    assert lines[0].index("TITLE") == lines[1].index("mm:")


# --- --since -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30d", REF - timedelta(days=30)),
        ("0d", REF),
        ("2026-10-01", datetime(2026, 10, 1, tzinfo=timezone.utc)),
    ],
    ids=["days", "zero-days", "date"],
)
def test_parse_since(text: str, expected: datetime) -> None:
    assert report.parse_since(text, REF) == expected


@pytest.mark.parametrize(
    "text",
    ["30", "-3d", "3w", "2026-13-01", "2026-10-1", "", "99999d"],
    ids=["no-unit", "negative", "weeks", "bad-month", "short-day", "empty", "too-many-days"],
)
def test_parse_since_refuses_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match="since"):
        report.parse_since(text, REF)


# --- show: looking up a commit -----------------------------------------------------


@pytest.fixture
def three() -> tuple[status.ItemState, ...]:
    return compute(
        [
            failed(H1, "6.1", "f1@gregkh"),
            failed(H2, "6.1", "f2@gregkh"),
            failed(H3, "6.6", "f3@gregkh"),
        ]
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (H1, (H1,)),
        (H1[:12], (H1,)),
        (H1[:12].upper(), (H1,)),
        (H2[:12], (H2, H3)),
        (H2[:13], (H2,)),
        ("f3@gregkh", (H3,)),
        ("<f3@gregkh>", (H3,)),
        (" f3@gregkh ", (H3,)),
        (H1[:11], ()),
        ("b1@x", ()),
    ],
    ids=[
        "full-hash",
        "prefix",
        "upper-case",
        "ambiguous",
        "longer-prefix",
        "message-id",
        "angle-brackets",
        "spaces",
        "too-short",
        "not-a-failed-report",
    ],
)
def test_lookup(
    three: tuple[status.ItemState, ...], query: str, expected: tuple[str, ...]
) -> None:
    assert report.lookup(three, query) == expected


def test_show_lines() -> None:
    states = compute(
        [
            failed(H1, "6.6", "f2@gregkh", days_ago=20, files=["a/b.c", "a/\x1b[2Jc.h"]),
            failed(H1, "5.10", "f1@gregkh", days_ago=20),
        ],
        backports=[backport("5.10", None, "b1@x", title="foo: fix the bar")],
        others=[OtherMail(env("r1@x", reply_to="f2@gregkh"))],
        entries=[entry(H1, "6.6", "releases/6.6.160")],
    )
    lines = report.show_lines(H1, states)
    text = "\n".join(lines)
    assert lines[0] == f"{H1} foo: fix the bar"
    assert lines[1] == "Author: Some Author <author@example.org>"
    assert lines[2] == "Files:  drivers/foo/bar.c a/b.c a/c.h"
    assert lines.index("5.10  backport posted (matched by title only)") < lines.index(
        "6.6   released in 6.6.160"
    )
    assert f"FAILED    2026-10-04  {PREFIX}f1@gregkh/" in text
    backport_line = (
        f"backport  2026-10-14  someone@example.org  {PREFIX}b1@x/  (matched by title only)"
    )
    assert backport_line in text
    assert f"reply     {PREFIX}r1@x/" in text
    assert "release   releases/6.6.160/a.patch" in text
    assert "\x1b" not in text


# --- export --json ---------------------------------------------------------------------

LORE = "https://lore.kernel.org/stable/git/2.git"
QUEUE = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/stable-queue.git"


def export_text() -> str:
    collected = mirror.Collected(
        "a" * 40,
        12,
        (
            failed(H1, "6.1", "f1@gregkh", days_ago=20, files=["a/b.c"]),
            failed(H1, "6.6", "f2@gregkh", days_ago=20),
            failed(H2, "6.1", "no-at-sign", title="bar: é \x1b[2J\x7f\x9b"),
        ),
        (backport("6.6", None, "b1@x", title="foo: fix the bar"),),
        (),
        (OtherMail(env("r1@x", reply_to="f1@gregkh")),),
        (mirror.Skipped("c" * 40, None, "too-large"),),
    )
    scan = queue.QueueScan("b" * 40, (entry(H2, "6.1", "releases/6.1.190"),), 1, 0, 0)
    info = store.SnapshotInfo(REF, True, REF, REF - timedelta(hours=4))
    doc = report.export_document(
        collected,
        scan,
        info,
        status.compute(collected, scan),
        generated_at=REF + timedelta(hours=1),
        tool="stable-backlog 9.9",
        lore_source=LORE,
        queue_source=QUEUE,
    )
    return report.export_json(doc)


def document() -> Any:
    """The export, read back the way a consumer would."""
    return json.loads(export_text())


def test_export_document_header() -> None:
    doc = document()
    assert doc["format"] == "stable-backlog/1"
    assert doc["generated_at"] == "2026-10-24T19:00:00+00:00"
    assert doc["tool"] == "stable-backlog 9.9"
    assert doc["snapshot"] == {
        "synced_at": "2026-10-24T18:00:00+00:00",
        "offline": True,
        "lore": {"url": LORE, "rev": "a" * 40, "newest": "2026-10-24T18:00:00+00:00"},
        "stable_queue": {"url": QUEUE, "rev": "b" * 40, "newest": "2026-10-24T14:00:00+00:00"},
    }
    assert doc["messages_read"] == 12
    assert doc["skipped"] == 1
    assert doc["skipped_samples"] == [
        {"commit": "c" * 40, "message_id": None, "reason": "too-large"}
    ]
    assert doc["counts"] == {"FAILED": 1, "BACKPORT_POSTED": 1, "QUEUED": 0, "RELEASED": 1}


def test_export_document_items() -> None:
    items = document()["items"]
    assert [(i["upstream"], i["tree"], i["status"]) for i in items] == [
        (H1, "6.1", "FAILED"),
        (H1, "6.6", "BACKPORT_POSTED"),
        (H2, "6.1", "RELEASED"),
    ]
    first, posted, released = items
    assert first == {
        "upstream": H1,
        "tree": "6.1",
        "status": "FAILED",
        "label": "no backport found",
        "confidence": None,
        "title": "foo: fix the bar",
        "author": {"name": "Some Author", "email": "author@example.org"},
        "files": ["a/b.c"],
        "first_failed": "2026-10-04T18:00:00+00:00",
        "failed": [
            {
                "message_id": "f1@gregkh",
                "date": "2026-10-04T18:00:00+00:00",
                "url": f"{PREFIX}f1@gregkh/",
            }
        ],
        "backports": [],
        "replies": [{"message_id": "r1@x", "url": f"{PREFIX}r1@x/"}],
        "queue": [],
        "release": None,
    }
    assert posted["confidence"] == "title"
    assert posted["label"] == "backport posted (matched by title only)"
    assert posted["backports"] == [
        {
            "message_id": "b1@x",
            "sender": "someone@example.org",
            "date": "2026-10-14T18:00:00+00:00",
            "confidence": "title",
            "url": f"{PREFIX}b1@x/",
        }
    ]
    assert released["failed"][0]["url"] is None  # no "@": no link
    assert released["queue"] == [
        {
            "location": "release",
            "directory": "releases/6.1.190",
            "path": "releases/6.1.190/a.patch",
            "release": "6.1.190",
        }
    ]
    assert released["release"] == "6.1.190"


def test_export_without_an_author_says_null() -> None:
    collected = mirror.Collected(
        "a" * 40, 1, (failed(H1, "6.1", "f1@gregkh", author=("", ""), files=()),), (), (), (), ()
    )
    scan = queue.QueueScan("b" * 40, (), 0, 0, 0)
    info = store.SnapshotInfo(REF, False, REF, REF)
    doc = report.export_document(
        collected,
        scan,
        info,
        status.compute(collected, scan),
        generated_at=REF,
        tool="t",
        lore_source=LORE,
        queue_source=QUEUE,
    )
    items = json.loads(report.export_json(doc))["items"]
    assert (items[0]["author"], items[0]["files"]) == (None, [])


def test_export_json_is_printable_ascii_only() -> None:
    # Titles hold anything anyone mailed: escapes, C1 controls, DEL, non-ASCII.
    text = export_text()
    assert text.endswith("\n")
    assert all(c == "\n" or " " <= c <= "~" for c in text)
    assert json.loads(text)["items"][2]["title"] == "bar: é \x1b[2J\x7f\x9b"
