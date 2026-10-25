# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the stable-backlog command (called as main(argv), no subprocess).

stdout carries only requested data, so sync writes nothing there; its
summary and all errors go to stderr, cleaned of terminal escape sequences.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stable_backlog import __version__, cli, gitio, sync
from tests.conftest import FIRST, LORE_URL, InboxFactory, Remotes
from tests.mailfactory import build_mail, failed_mail

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
ESCAPES = "\x1b]0;owned\x07\x1b[2J\x1b[31m"


def test_sync_builds_the_snapshot_and_prints_a_summary(
    lore_remote: Path, queue_remote: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW) == 0

    out, err = capsys.readouterr()
    assert out == ""
    assert (cache_dir / sync.SNAPSHOT).is_file()
    assert "lore stable epoch 2: cloned" in err
    assert "stable-queue: cloned" in err
    assert "1 message read: 1 FAILED report, 0 backports" in err
    assert str(cache_dir / sync.SNAPSHOT) in err


def test_second_sync_reports_unchanged_mirrors(
    lore_remote: Path, queue_remote: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW)
    capsys.readouterr()
    later = NOW + sync.MIN_FETCH_INTERVAL
    assert cli.main(["sync", "--cache-dir", str(cache_dir)], now=later) == 0
    assert "lore stable epoch 2: unchanged at" in capsys.readouterr().err


def test_second_network_sync_within_an_hour_exits_1_and_points_to_offline(
    lore_remote: Path, queue_remote: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW)
    capsys.readouterr()
    soon = NOW + timedelta(minutes=10)
    assert cli.main(["sync", "--cache-dir", str(cache_dir)], now=soon) == 1
    err = capsys.readouterr().err
    assert err.startswith("stable-backlog: error: ")
    assert "at most once an hour" in err
    assert "--offline" in err


def test_default_cache_dir_follows_xdg(
    lore_remote: Path, queue_remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cli.main(["sync"], now=NOW) == 0
    assert (tmp_path / "xdg" / "stable-backlog" / sync.SNAPSHOT).is_file()


def test_offline_sync_says_so(
    lore_remote: Path, queue_remote: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW)
    capsys.readouterr()
    assert cli.main(["sync", "--offline", "--cache-dir", str(cache_dir)], now=NOW) == 0
    assert "offline rebuild" in capsys.readouterr().err


def test_skipped_messages_are_counted_with_their_message_ids(
    make_inbox: InboxFactory,
    remotes: Remotes,
    queue_remote: Path,
    cache_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = failed_mail(body="broken\n", message_id="broken@gregkh")
    remotes.point(LORE_URL, make_inbox([FIRST, broken]))
    assert cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW) == 0
    err = capsys.readouterr().err
    assert "skipped 1 message: broken@gregkh (" in err


def test_failure_exits_1_with_one_error_line(
    remotes: Remotes,
    queue_remote: Path,
    cache_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    remotes.point(LORE_URL, tmp_path / "gone.git")
    assert cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("stable-backlog: error: cannot clone lore stable epoch 2")
    assert err.count("\n") == 1


def test_error_text_is_cleaned_for_the_terminal(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def hostile(*args: object, **kwargs: object) -> sync.SyncResult:
        logging.getLogger("stable_backlog.sync").warning("remote said %s", ESCAPES)
        raise sync.SyncError(f"remote said {ESCAPES}\nsecond line")

    monkeypatch.setattr(sync, "sync", hostile)
    assert cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW) == 1
    err = capsys.readouterr().err
    assert "remote said" in err
    assert "\x1b" not in err
    assert "\x07" not in err
    assert "second line" in err


# --- list and show ---------------------------------------------------------------

H1 = "1111111111111111111111111111111111111111"
H2 = "2222222222222222222222222222222222222222"
TTY = "b48820de8c1a596c5b99775524dfce73c6576c5d"  # queued for 6.1 in the golden stable-queue


def list_mails() -> list[bytes]:
    """Two commits with no backport found (three requests), one queued."""
    return [
        failed_mail(
            H1,
            "6.1",
            "f1@gregkh",
            "mm: fix the old one",
            date="Fri, 04 Sep 2026 12:00:00 +0000",  # 50 days before the newest message
            author="Ann Author <ann@example.org>",
            files=["mm/old.c"],
        ),
        failed_mail(
            H1,
            "5.15",
            "f2@gregkh",
            "mm: fix the old one",
            date="Sat, 05 Sep 2026 12:00:00 +0000",
            author="Ann Author <ann@example.org>",
            files=["mm/old.c"],
        ),
        failed_mail(
            H2,
            "6.6",
            "f3@gregkh",
            "net: fix the newer one",
            date="Wed, 14 Oct 2026 12:00:00 +0000",  # 10 days
            author="Bob <bob@example.org>",
            files=["net/core/new.c"],
        ),
        build_mail(
            "Re: FAILED: patch",
            "not needed for 6.6\n",
            message_id="<r1@example.org>",
            in_reply_to="<f3@gregkh>",
            date="Thu, 15 Oct 2026 12:00:00 +0000",
        ),
        failed_mail(TTY, "6.1", "f4@gregkh", date="Mon, 05 Oct 2026 12:00:00 +0000"),
    ]


@pytest.fixture
def snapshot(
    make_inbox: InboxFactory,
    remotes: Remotes,
    queue_remote: Path,
    cache_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> Path:
    """A snapshot built by sync from list_mails() and the golden stable-queue."""
    remotes.point(LORE_URL, make_inbox(list_mails()))
    assert cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW) == 0
    capsys.readouterr()
    return cache_dir / sync.SNAPSHOT


def run(cache_dir: Path, *argv: str, now: datetime = NOW) -> int:
    return cli.main([*argv, "--cache-dir", str(cache_dir)], now=now)


def test_list_shows_commits_with_no_backport_found_oldest_first(
    snapshot: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(cache_dir, "list") == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0].split() == ["AGE", "UPSTREAM", "TREES", "REPLIES", "TITLE"]
    assert lines[1].split() == [
        "50d",
        H1[:12],
        "5.15",
        "6.1",
        "-",
        "mm:",
        "fix",
        "the",
        "old",
        "one",
    ]
    assert lines[2].split()[:5] == ["10d", H2[:12], "6.6", "1", "net:"]
    assert len(lines) == 3  # TTY is queued for 6.1
    assert "stable-backlog: 3 requests in 2 commits: no backport found\n" in err
    assert "lore up to 2026-10-24 12:00 UTC" in err
    assert "stable-backlog show" in err
    assert "warning" not in err


@pytest.mark.parametrize(
    ("args", "rows"),
    [
        (["--tree", "6.6"], [H2]),
        (["--tree", "5.15", "--tree", "6.6"], [H1, H2]),
        (["--status", "queued"], [TTY]),
        (["--status", "queued,failed"], [H1, TTY, H2]),
        (["--all"], [H1, TTY, H2]),
        (["--since", "30d"], [H2]),
        (["--author", "ANN"], [H1]),
        (["--author", "bob@example.org"], [H2]),
        (["--path", "net/core"], [H2]),
        (["--path", "mm/old.c", "--tree", "6.1"], [H1]),
    ],
    ids=[
        "tree",
        "two-trees",
        "status",
        "two-statuses",
        "all",
        "since-days",
        "author-name",
        "author-address",
        "path-directory",
        "path-and-tree",
    ],
)
def test_list_filters(
    snapshot: Path,
    cache_dir: Path,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    rows: list[str],
) -> None:
    assert run(cache_dir, "list", *args) == 0
    out = capsys.readouterr().out
    assert [line.split()[1] for line in out.splitlines()[1:]] == [h[:12] for h in rows]


def test_list_trees_column_shows_the_trees_that_match(
    snapshot: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --since is checked per request: the 6.1 report came a day before.
    assert run(cache_dir, "list", "--since", "2026-09-05") == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[1].split()[:3] == ["49d", H1[:12], "5.15"]
    assert "2 requests in 2 commits" in err


def test_list_with_no_match_prints_nothing_and_says_so(
    snapshot: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(cache_dir, "list", "--tree", "4.19") == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "no requests match" in err


def test_list_reports_requests_a_filter_could_not_check(
    make_inbox: InboxFactory,
    remotes: Remotes,
    queue_remote: Path,
    cache_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    no_author = failed_mail(H1, "6.1", "f1@gregkh", author="nobody")
    remotes.point(LORE_URL, make_inbox([no_author, failed_mail(H2, "6.1", "f2@gregkh")]))
    cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW)
    capsys.readouterr()
    assert run(cache_dir, "list", "--author", "author") == 0
    out, err = capsys.readouterr()
    assert [line.split()[1] for line in out.splitlines()[1:]] == [H2[:12]]
    assert "--author: 1 request has no author and was left out" in err


def test_list_and_show_report_skipped_messages(
    make_inbox: InboxFactory,
    remotes: Remotes,
    queue_remote: Path,
    cache_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = failed_mail(body="broken\n", message_id="broken@gregkh")
    remotes.point(LORE_URL, make_inbox([FIRST, broken]))
    cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW)
    capsys.readouterr()
    for argv in (["list"], ["show", "b48820de8c1a"]):
        assert run(cache_dir, *argv) == 0
        err = capsys.readouterr().err
        assert "1 message could not be read when the snapshot was built: broken@gregkh (" in err


def test_list_warns_when_the_snapshot_is_old(
    snapshot: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(cache_dir, "list", now=NOW + timedelta(days=3)) == 0
    err = capsys.readouterr().err
    assert "warning: the newest lore message in the snapshot is 4.0 days old" in err
    assert "stable-backlog sync" in err


def test_list_says_when_the_snapshot_was_rebuilt_offline(
    snapshot: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(cache_dir, "sync", "--offline") == 0
    capsys.readouterr()
    assert run(cache_dir, "list") == 0
    assert "(offline rebuild)" in capsys.readouterr().err


def test_list_cleans_titles_for_the_terminal(
    make_inbox: InboxFactory,
    remotes: Remotes,
    queue_remote: Path,
    cache_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    title = f"mm: {ESCAPES}fix\x9b it"
    remotes.point(LORE_URL, make_inbox([failed_mail(H1, "6.1", "f1@gregkh", title)]))
    cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW)
    capsys.readouterr()
    assert run(cache_dir, "list") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[1].endswith("mm: fix it")
    assert not any(c in out for c in "\x1b\x07\x9b")


def test_golden_requests_all_have_a_backport_so_list_shows_none(
    make_inbox: InboxFactory,
    remotes: Remotes,
    queue_remote: Path,
    cache_dir: Path,
    golden_mails: list[bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Every golden request was answered: listing one would be a false report.
    remotes.point(LORE_URL, make_inbox(golden_mails))
    cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW)
    capsys.readouterr()
    assert run(cache_dir, "list") == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "no requests match" in err
    assert run(cache_dir, "list", "--all") == 0
    assert len(capsys.readouterr().out.splitlines()) == 1 + 5  # gso, pmd, acpi, tty, damon


def test_golden_show(
    make_inbox: InboxFactory,
    remotes: Remotes,
    queue_remote: Path,
    cache_dir: Path,
    golden_mails: list[bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    remotes.point(LORE_URL, make_inbox(golden_mails))
    cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW)
    capsys.readouterr()
    assert run(cache_dir, "show", TTY[:12]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"{TTY} tty: port: fix tty_port_tty_vhangup() race"
    assert lines[1] == "Author: Johan Hovold <johan@kernel.org>"
    assert lines[2] == "Files:  drivers/tty/tty_port.c include/linux/tty_port.h"
    heads = [line for line in lines if line[:1].isdigit()]
    assert heads == [
        "5.10  queued (queue-5.10)",
        "5.15  queued (queue-5.15)",
        "6.1   queued (queue-6.1)",
        "6.6   released in 6.6.160",
        "6.12  released in 6.12.113",
    ]
    assert (
        "      FAILED    2026-10-05  https://lore.kernel.org/stable/"
        "2026100552-unscathed-jailer-aa56@gregkh/" in lines
    )


@pytest.mark.parametrize(
    "query", [H2[:12], "f3@gregkh", "<f3@gregkh>"], ids=["prefix", "message-id", "angle"]
)
def test_show_one_commit(
    snapshot: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str], query: str
) -> None:
    assert run(cache_dir, "show", query) == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0] == f"{H2} net: fix the newer one"
    assert lines[1] == "Author: Bob <bob@example.org>"
    assert "6.6   no backport found" in lines
    assert "      reply     https://lore.kernel.org/stable/r1@example.org/" in lines
    assert "lore up to" in err


def test_show_ambiguous_prefix_lists_the_commits(
    make_inbox: InboxFactory,
    remotes: Remotes,
    queue_remote: Path,
    cache_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = "1111111111119999999999999999999999999999"
    mails = [failed_mail(H1, "6.1", "f1@gregkh"), failed_mail(other, "6.1", "f2@gregkh")]
    remotes.point(LORE_URL, make_inbox(mails))
    cli.main(["sync", "--cache-dir", str(cache_dir)], now=NOW)
    capsys.readouterr()
    assert run(cache_dir, "show", H1[:12]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert f"{H1[:12]} matches 2 commits: {H1}, {other}" in err


@pytest.mark.parametrize(
    "query", ["3333333333333333", "nope@example.org", "1111"], ids=["hash", "message-id", "short"]
)
def test_show_not_found_exits_1_and_says_what_the_snapshot_covers(
    snapshot: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str], query: str
) -> None:
    assert run(cache_dir, "show", query) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("stable-backlog: error: no FAILED report for")
    assert "2026-09-04 to 2026-10-14" in err


def test_list_and_show_read_the_snapshot_only(
    snapshot: Path, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_git(*args: object, **kwargs: object) -> None:
        raise AssertionError("git must not run")

    for name in ("run", "popen", "network_run"):
        monkeypatch.setattr(gitio, name, no_git)
    before = {p.name: p.read_bytes() for p in cache_dir.iterdir() if p.is_file()}
    for argv in (["list"], ["list", "--all"], ["show", H1[:12]], ["show", "nope@x"]):
        run(cache_dir, *argv)
    after = {p.name: p.read_bytes() for p in cache_dir.iterdir() if p.is_file()}
    assert after == before


def test_list_without_a_snapshot_asks_for_sync(
    cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(cache_dir, "list") == 1
    err = capsys.readouterr().err
    assert err.startswith("stable-backlog: error: no snapshot at ")
    assert "run `stable-backlog sync` first" in err
    assert not cache_dir.exists()  # reading never creates the cache


def test_list_with_an_old_snapshot_asks_for_sync(
    snapshot: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with contextlib.closing(sqlite3.connect(snapshot)) as conn, conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    assert run(cache_dir, "list") == 1
    assert "schema version 1" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["frobnicate"],
        ["sync", "--offline", "--reclone", "lore"],
        ["sync", "--reclone", "linux"],
        ["list", "--tree", "6"],
        ["list", "--tree", "6.1; rm -rf ~"],
        ["list", "--since", "yesterday"],
        ["list", "--status", "nobody-working"],
        ["list", "--status", "failed", "--all"],
        ["list", "--path", "/"],
        ["show"],
    ],
    ids=[
        "no-command",
        "unknown-command",
        "offline-with-reclone",
        "unknown-source",
        "bad-tree",
        "tree-injection",
        "bad-since",
        "bad-status",
        "status-and-all",
        "empty-path",
        "show-without-query",
    ],
)
def test_usage_errors_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv, now=NOW)
    assert exc.value.code == 2
    assert "usage: stable-backlog" in capsys.readouterr().err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"stable-backlog {__version__}"
