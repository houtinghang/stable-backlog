# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the web page (export --html).

The page is read back with the standard library's HTML parser, the way a
browser would see it: structure, links, the Content-Security-Policy and the
text. Everything from messages is assumed to be written by an attacker.
"""

from __future__ import annotations

import base64
import hashlib
import html.parser
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta

import pytest

from stable_backlog import mirror, page, status, store
from stable_backlog.model import BackportPost, FailedRequest, OtherMail, QueueEntry
from stable_backlog.sanitize import for_terminal
from tests.records import REF, backport, collected, entry, env, failed, scan

H1 = "1111111111111111111111111111111111111111"
H2 = "2222222222222222222222222222222222222222"
H3 = "3333333333333333333333333333333333333333"
H4 = "4444444444444444444444444444444444444444"
H5 = "5555555555555555555555555555555555555555"
GENERATED = REF + timedelta(hours=1)
LORE = "https://lore.kernel.org/stable/"
LINUX = "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id="
QUEUE = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/stable-queue.git/commit/?id="
PROJECT = "https://github.com/houtinghang/stable-backlog"
JSON_FILE = "stable-backlog.json"

VOID = {"meta"}
TAGS = {
    "html",
    "head",
    "meta",
    "title",
    "style",
    "body",
    "h1",
    "h2",
    "p",
    "nav",
    "a",
    "div",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "code",
    "bdi",
    "strong",
    "footer",
}
ATTRIBUTES = {"lang", "charset", "http-equiv", "content", "name", "href", "id", "class", "scope"}


@dataclass
class Cell:
    text: str = ""
    hrefs: list[str] = field(default_factory=list)


class Page(html.parser.HTMLParser):
    """What a browser would see: tags, text (with its parent tag), table rows."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.raw = text
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.texts: list[tuple[str, str]] = []  # (parent tag, text)
        self.errors: list[str] = []
        self.declarations: list[str] = []
        self.style = ""
        self.rows: list[tuple[str, bool, list[Cell]]] = []  # (section id, in <thead>, cells)
        self._stack: list[str] = []
        self._section = ""
        self.feed(text)
        self.close()
        if self._stack:
            self.errors.append(f"not closed: {self._stack}")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        self.tags.append((tag, values))
        if tag not in VOID:
            self._stack.append(tag)
        if tag == "h2":
            self._section = values.get("id", "")
        elif tag == "tr":
            self.rows.append((self._section, self._in("thead"), []))
        elif tag in ("td", "th"):
            self.rows[-1][2].append(Cell())
        elif tag == "a" and self._in("tr") and self.rows[-1][2]:
            self.rows[-1][2][-1].hrefs.append(values.get("href", ""))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.errors.append(f"self-closing <{tag}/>")

    def handle_endtag(self, tag: str) -> None:
        if not self._stack or self._stack[-1] != tag:
            self.errors.append(f"</{tag}> does not close {self._stack[-1:]}")
            return
        self._stack.pop()

    def handle_data(self, data: str) -> None:
        parent = self._stack[-1] if self._stack else ""
        if parent == "style":
            self.style += data
            return
        self.texts.append((parent, data))
        if self._in("td") or self._in("th"):
            self.rows[-1][2][-1].text += data

    def handle_comment(self, data: str) -> None:
        self.errors.append(f"comment {data!r:.40}")

    def handle_decl(self, decl: str) -> None:
        self.declarations.append(decl)

    def _in(self, tag: str) -> bool:
        return tag in self._stack

    @property
    def text(self) -> str:
        return " ".join(" ".join(t for _, t in self.texts).split())

    def section(self, section: str) -> list[list[Cell]]:
        """Body rows of the table after <h2 id=section>."""
        return [cells for s, head, cells in self.rows if s == section and not head]

    def hrefs(self) -> list[str]:
        return [a.get("href", "") for t, a in self.tags if t == "a"]


def render(
    failed_: Sequence[FailedRequest],
    backports: Sequence[BackportPost] = (),
    others: Sequence[OtherMail] = (),
    entries: Sequence[QueueEntry] = (),
    skipped: Sequence[mirror.Skipped] = (),
    *,
    offline: bool = False,
) -> str:
    col = collected(failed_, backports, others, skipped)
    sc = scan(entries)
    info = store.SnapshotInfo(GENERATED, offline, REF, REF - timedelta(hours=4))
    states = status.compute(col, sc)
    return page.render(col, sc, info, states, generated_at=GENERATED, tool="stable-backlog 9.9")


def sample(skipped: Sequence[mirror.Skipped] = (), *, offline: bool = False) -> str:
    """Two commits with no backport found (three requests), one posted, one released,
    and a tree (7.1) whose only request is queued."""
    return render(
        [
            failed(H1, "5.10", "f1@gregkh", days_ago=40, title="mm: fix the old one"),
            failed(H1, "6.1", "f2@gregkh", days_ago=39, title="mm: fix the old one"),
            failed(H2, "6.1", "f3@gregkh", days_ago=5, title="net: fix the new one"),
            failed(H2, "6.1", "f3b@gregkh", days_ago=4, title="net: fix the new one"),
            failed(H3, "6.6", "f4@gregkh", days_ago=20),
            failed(H4, "6.6", "f5@gregkh", days_ago=30),
            failed(H5, "7.1", "f6@gregkh", days_ago=10),
        ],
        backports=[backport("6.6", H3, "b1@x")],
        others=[OtherMail(env("r1@x", reply_to="f2@gregkh"))],
        entries=[entry(H4, "6.6", "releases/6.6.100"), entry(H5, "7.1", "queue-7.1")],
        skipped=skipped,
        offline=offline,
    )


def texts(cells: list[Cell]) -> list[str]:
    return [" ".join(c.text.split()) for c in cells]


# --- the document and its Content-Security-Policy --------------------------------


def test_page_is_well_formed_printable_ascii() -> None:
    text = sample()
    assert text.startswith("<!DOCTYPE html>\n")
    assert text.endswith("</html>\n")
    assert all(c == "\n" or " " <= c <= "~" for c in text)
    p = Page(text)
    assert p.errors == []
    assert p.declarations == ["DOCTYPE html"]
    assert p.tags[0] == ("html", {"lang": "en"})


def test_csp_comes_first_and_allows_only_the_page_style() -> None:
    p = Page(sample())
    names = [t for t, _ in p.tags]
    assert names[:4] == ["html", "head", "meta", "meta"]
    assert p.tags[2][1] == {"charset": "utf-8"}
    csp = p.tags[3][1]
    assert csp["http-equiv"] == "Content-Security-Policy"
    digest = base64.b64encode(hashlib.sha256(p.style.encode("ascii")).digest()).decode()
    assert [d.strip() for d in csp["content"].split(";")] == [
        "default-src 'none'",
        f"style-src 'sha256-{digest}'",
        "base-uri 'none'",
        "form-action 'none'",
    ]
    assert names.index("style") > 3
    assert p.style.strip()


def test_no_scripts_handlers_or_inline_styles() -> None:
    p = Page(sample())
    assert {t for t, _ in p.tags} <= TAGS
    attributes = {name for _, attrs in p.tags for name in attrs}
    assert attributes <= ATTRIBUTES
    assert "<script" not in p.raw.lower()


def test_links_go_only_to_lore_git_kernel_org_and_the_project() -> None:
    p = Page(sample())
    ids = {a["id"] for _, a in p.tags if "id" in a}
    for href in p.hrefs():
        if href.startswith("#"):
            assert href[1:] in ids
        else:
            assert href.startswith((LORE, LINUX, QUEUE)) or href in (PROJECT, JSON_FILE), href


# --- what the page says ----------------------------------------------------------


def test_notice_and_wording() -> None:
    text = Page(sample()).text
    assert "Not an official kernel.org or stable team tool." in text
    assert "have not been checked by hand yet" in text
    assert "not that nobody is working on it" in text
    assert text.count("nobody") == 1


def test_data_times_and_dates_covered() -> None:
    text = Page(sample()).text
    assert "lore stable archive up to 2026-10-24 18:00 UTC" in text
    assert "stable-queue 000000000000 (2026-10-24 14:00 UTC)" in text
    assert "FAILED reports from 2026-09-14 to 2026-10-20" in text
    assert "Page built 2026-10-24 19:00 UTC" in text
    assert "more than two days old" in text
    assert "without fetching" not in text
    assert "rebuilt from the local mirrors without fetching" in Page(sample(offline=True)).text


def test_summary_counts_every_status_per_tree() -> None:
    rows = Page(sample()).section("summary")
    assert [texts(r) for r in rows] == [
        ["5.10", "1", "0", "0", "0"],
        ["6.1", "2", "0", "0", "0"],
        ["6.6", "0", "1", "0", "1"],
        ["7.1", "0", "0", "1", "0"],
        ["All", "3", "1", "1", "1"],
    ]
    assert rows[0][0].hrefs == ["#tree-5.10"]


def test_all_trees_one_row_per_commit_longest_waiting_first() -> None:
    p = Page(sample())
    assert "All trees: 2 commits with no backport found" in p.text
    rows = p.section("all")
    assert [texts(r) for r in rows] == [
        ["40d", H1[:12], "mm: fix the old one", "Some Author", "5.10 6.1", "1 reply"],
        ["5d", H2[:12], "net: fix the new one", "Some Author", "6.1", ""],
    ]
    assert rows[0][1].hrefs == [LINUX + H1]
    assert rows[0][4].hrefs == [LORE + "f1@gregkh/", LORE + "f2@gregkh/"]


def test_each_tree_has_a_section_with_every_failed_report_and_reply() -> None:
    p = Page(sample())
    assert "6.1: 2 requests with no backport found" in p.text
    rows = p.section("tree-6.1")
    assert [texts(r) for r in rows] == [
        ["39d", H1[:12], "mm: fix the old one", "Some Author", "2026-09-15", "reply 1"],
        ["5d", H2[:12], "net: fix the new one", "Some Author", "2026-10-19 2026-10-20", ""],
    ]
    assert rows[0][5].hrefs == [LORE + "r1@x/"]
    assert rows[1][4].hrefs == [LORE + "f3@gregkh/", LORE + "f3b@gregkh/"]
    assert texts(p.section("tree-5.10")[0])[:2] == ["40d", H1[:12]]


def test_replies_say_the_request_may_already_be_answered() -> None:
    assert "may already be answered" in Page(sample()).text
    no_replies = render([failed(H1, "6.1", "f1@gregkh")])
    assert "may already be answered" not in Page(no_replies).text


def test_trees_without_open_requests_say_none() -> None:
    p = Page(sample())
    assert "6.6: 0 requests with no backport found" in p.text
    assert "7.1: 0 requests with no backport found" in p.text
    assert p.section("tree-7.1") == []
    nav = [a["href"] for t, a in p.tags if t == "a" and a["href"].startswith("#tree-")]
    assert nav[:4] == ["#tree-5.10", "#tree-6.1", "#tree-6.6", "#tree-7.1"]


def test_empty_snapshot() -> None:
    p = Page(render([]))
    assert p.errors == []
    assert "All trees: 0 commits with no backport found" in p.text
    assert "no dated FAILED reports" in p.text


def test_no_email_addresses() -> None:
    # Decision 2026-10-25 (4,a): names only on the page; addresses are in the JSON.
    text = sample()
    assert "author@example.org" not in text
    assert "someone@example.org" not in text


def test_undated_request_has_an_unknown_age() -> None:
    p = Page(render([failed(H1, "6.1", "f1@gregkh", days_ago=None)]))
    assert texts(p.section("all")[0])[0] == "?"
    assert texts(p.section("tree-6.1")[0])[4] == "date unknown"


def test_message_id_without_at_sign_gets_no_link() -> None:
    p = Page(render([failed(H1, "6.1", "no-at-sign", days_ago=3)]))
    assert p.section("all")[0][4].hrefs == []
    assert p.section("tree-6.1")[0][4].hrefs == []
    assert all(not h.startswith(LORE) for h in p.hrefs())


def test_skipped_messages_are_shown_at_the_top_and_listed_at_the_end() -> None:
    skipped = [
        mirror.Skipped("c" * 40, None, "too-large"),
        mirror.Skipped("d" * 40, "bad@gregkh", "FAILED subject not understood"),
    ]
    p = Page(sample(skipped=skipped))
    assert "2 messages could not be read when the data was built" in p.text
    assert f"commit {'c' * 12} (too-large)" in p.text
    assert "bad@gregkh (FAILED subject not understood)" in p.text
    assert LORE + "bad@gregkh/" in p.hrefs()
    assert "could not be read" not in Page(sample()).text


def test_at_most_20_skipped_samples() -> None:
    skipped = [mirror.Skipped(f"{i:040x}", f"m{i}@x", "too-large") for i in range(25)]
    p = Page(sample(skipped=skipped))
    assert "25 messages could not be read" in p.text
    assert "m19@x" in p.text
    assert "m20@x" not in p.text
    assert "and 5 more" in p.text


def test_footer_links_the_json_and_the_project() -> None:
    p = Page(sample())
    assert JSON_FILE in p.hrefs()
    assert PROJECT in p.hrefs()
    assert "stable-backlog 9.9" in p.text


def test_same_input_gives_the_same_page() -> None:
    assert sample() == sample()


def test_non_ascii_becomes_character_references() -> None:
    raw = render([failed(H1, "6.1", "f1@gregkh", title="drm: é", author=("Jörg Rödel", "j@x"))])
    assert "drm: &#233;" in raw
    p = Page(raw)
    assert "drm: é" in p.text
    assert "Jörg Rödel" in p.text


def test_a_tree_name_that_is_not_a_version_is_refused() -> None:
    # The parser only produces "X.Y"; a tree name is used in id="tree-X.Y".
    col = collected([failed(H1, "6.1", "f1@gregkh")])
    sc = scan()
    states = tuple(replace(s, tree='6.1" onclick="x') for s in status.compute(col, sc))
    info = store.SnapshotInfo(GENERATED, False, REF, REF)
    with pytest.raises(ValueError, match="tree"):
        page.render(col, sc, info, states, generated_at=GENERATED, tool="t")


# --- hostile text from messages --------------------------------------------------

HOSTILE = [
    "<script>alert(1)</script>",
    "</td></tr></table><img src=x onerror=alert(1)>",
    '"><a href="javascript:alert(1)">x</a>',
    "' onmouseover='alert(1)",
    "<!-- x",
    "<style>body{display:none}</style>",
    "&amp;&lt;&#60;",
    "‮evil‬",
    "nul\x00 esc\x1b[2J c1\x9b del\x7f tab\tnl\n",
    "x" * 5000,
]
HOSTILE_IDS = [
    "script",
    "close-table",
    "quote-attribute",
    "single-quote",
    "comment",
    "style",
    "entities",
    "bidi",
    "controls",
    "long",
]


def hostile_page(text: str) -> str:
    mid = f"{text}@evil"
    return render(
        [
            failed(H1, "6.1", mid, days_ago=3, title=text, author=(text, "a@x")),
            failed(H2, "6.6", "f2@gregkh", days_ago=2, title=f"{text}: second"),
        ],
        others=[OtherMail(env(f"re-{text}@evil", reply_to=mid))],
        skipped=[mirror.Skipped("c" * 40, f"skip-{text}@evil", f"reason {text}")],
    )


@pytest.mark.parametrize("text", HOSTILE, ids=HOSTILE_IDS)
def test_hostile_text_cannot_change_the_page_structure(text: str) -> None:
    hostile = Page(hostile_page(text))
    benign = Page(hostile_page("benign"))
    assert hostile.errors == []
    assert [t for t, _ in hostile.tags] == [t for t, _ in benign.tags]
    assert [sorted(a) for _, a in hostile.tags] == [sorted(a) for _, a in benign.tags]
    assert hostile.style == benign.style
    for href in hostile.hrefs():
        assert href.startswith(("#", LORE, LINUX, QUEUE)) or href in (PROJECT, JSON_FILE)


@pytest.mark.parametrize("text", HOSTILE, ids=HOSTILE_IDS)
def test_hostile_text_is_shown_as_text(text: str) -> None:
    p = Page(hostile_page(text))
    shown = " ".join(for_terminal(text).split())
    assert shown in p.text
    titles = [t for parent, t in p.texts if t.startswith(for_terminal(text)[:20])]
    assert titles
    assert all(parent == "bdi" for parent, t in p.texts if t == for_terminal(text))
