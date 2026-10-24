# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the stable-backlog command (called as main(argv), no subprocess).

stdout carries only requested data, so sync writes nothing there; its
summary and all errors go to stderr, cleaned of terminal escape sequences.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stable_backlog import __version__, cli, sync
from tests.conftest import FIRST, LORE_URL, InboxFactory, Remotes
from tests.mailfactory import failed_mail

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


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["frobnicate"],
        ["sync", "--offline", "--reclone", "lore"],
        ["sync", "--reclone", "linux"],
    ],
    ids=["no-command", "unknown-command", "offline-with-reclone", "unknown-source"],
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
