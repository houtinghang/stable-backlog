# SPDX-License-Identifier: GPL-2.0-or-later
"""Malicious-input tests. Every message is assumed to be written by an attacker.

Each test names the rule it covers (CLAUDE.md section 3 / docs/DESIGN.md
"Security model").
"""

from __future__ import annotations

import contextlib
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from stable_backlog import gitio, mirror, store
from stable_backlog.model import BackportPost, FailedRequest, OtherMail, ReviewMail
from stable_backlog.parse import MAX_MESSAGE_BYTES, MessageTooLarge, ParseError, parse_message
from stable_backlog.sanitize import for_terminal
from tests.conftest import InboxFactory
from tests.mailfactory import GREG, build_mail, failed_body, failed_mail

TTY = "b48820de8c1a596c5b99775524dfce73c6576c5d"
SHELL_PAYLOADS = [
    "$(touch PWNED)",
    "`touch PWNED`",
    "; touch PWNED",
    "| touch PWNED",
    "--upload-pack=touch PWNED",
    "-c core.sshCommand=touch PWNED",
]
SHELL_IDS = ["dollar-paren", "backtick", "semicolon", "pipe", "upload-pack", "dash-c"]


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every subprocess started (clear it before the code under test)."""
    calls: list[dict[str, Any]] = []
    real_popen = subprocess.Popen
    real_run = subprocess.run

    def spy_popen(args: Any, *a: Any, **kw: Any) -> Any:
        calls.append({"args": args, **kw})
        return real_popen(args, *a, **kw)

    def spy_run(args: Any, *a: Any, **kw: Any) -> Any:
        calls.append({"args": args, **kw})
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    monkeypatch.setattr(subprocess, "run", spy_run)
    return calls


# --- rule 1/2: never execute message content; subprocess hygiene ------------


@pytest.mark.parametrize("payload", SHELL_PAYLOADS, ids=SHELL_IDS)
def test_shell_payloads_in_headers_are_inert(
    payload: str,
    make_inbox: InboxFactory,
    git_calls: list[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    failed_id = "evil" + payload.replace(" ", "") + "@gregkh"
    mails = [
        failed_mail(title=f"x: {payload}", message_id=failed_id),
        build_mail(
            f"[PATCH 6.6.y] x: {payload}",
            f"[ Upstream commit {TTY} ]\n{payload}\n",
            sender=f"{payload} <a@example.org>",
            message_id="<" + payload.replace(" ", "") + "@example.org>",
            in_reply_to=f"<{failed_id}>",
        ),
    ]
    inbox = make_inbox(mails)
    git_calls.clear()  # only count what stable_backlog itself runs
    result = mirror.collect(inbox, max_count=10)
    assert result.skipped == ()
    assert [f.envelope.message_id for f in result.failed] == [failed_id]
    assert len(result.backports) == 1
    assert not (tmp_path / "PWNED").exists()
    assert git_calls, "expected git to be called"
    assert all(call["args"][0] == gitio.git_executable() for call in git_calls)
    for call in git_calls:
        assert isinstance(call["args"], list)
        assert call.get("shell", False) is False
        for arg in call["args"]:
            assert payload not in arg
            assert "touch" not in arg


@pytest.mark.parametrize(
    "line",
    [
        f"git cherry-pick -x {TTY}; rm -rf ~",
        "git cherry-pick -x --upload-pack=evil",
        f"git cherry-pick -x {TTY[:39]}",
        f"git cherry-pick -x {TTY}0",
        f"git cherry-pick -x {TTY.upper()}",
    ],
    ids=["trailing-command", "option-injection", "too-short", "too-long", "uppercase"],
)
def test_fake_hash_in_failed_is_rejected(line: str) -> None:
    body = failed_body(TTY, "6.6", "x@gregkh", "t").replace(f"git cherry-pick -x {TTY}", line)
    with pytest.raises(ParseError, match="cherry-pick"):
        parse_message(failed_mail(body=body))


def test_validate_hash_rejects_options_and_garbage() -> None:
    assert gitio.validate_hash(TTY) == TTY
    for bad in ["--all", "HEAD", "", TTY[:11], TTY + "0", TTY.upper(), f"{TTY}\n"]:
        with pytest.raises(ValueError, match="hash"):
            gitio.validate_hash(bad)


# --- rule 5: file names never come from message content -----------------------


@pytest.mark.parametrize(
    "mid",
    ["../../etc/passwd@x", "/etc/passwd@x", "..\\..\\x@y", "a/../../b@c"],
    ids=["dotdot", "absolute", "backslash", "nested"],
)
def test_path_like_message_id_creates_no_files(
    mid: str, make_inbox: InboxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    inbox = make_inbox([failed_mail(message_id=mid)])
    result = mirror.collect(inbox, max_count=10)
    assert [f.envelope.message_id for f in result.failed] == [mid]
    after = sorted(p.name for p in tmp_path.iterdir())
    assert set(after) - set(before) == {inbox.name}


# --- rule 6: parsing limits; failures are counted, never silent --------------


def test_oversized_message_is_skipped_and_counted(make_inbox: InboxFactory) -> None:
    big = build_mail("[PATCH 6.1.y] big", "x" * (MAX_MESSAGE_BYTES + 1))
    ok = failed_mail()
    result = mirror.collect(make_inbox([big, ok]), max_count=10)
    assert len(result.failed) == 1
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "too-large"
    with pytest.raises(MessageTooLarge):
        parse_message(big)


def test_malformed_failed_is_skipped_with_message_id(make_inbox: InboxFactory) -> None:
    bad = failed_mail(body="no command here\n", message_id="broken@gregkh")
    result = mirror.collect(make_inbox([bad]), max_count=10)
    assert result.failed == ()
    assert len(result.skipped) == 1
    assert result.skipped[0].message_id == "broken@gregkh"
    assert "cherry-pick" in result.skipped[0].reason


def test_huge_single_line_subject_is_fast() -> None:
    raw = build_mail("FAILED: patch " + '"' * 200_000 + "a" * 300_000, "x\n" * 10, sender=GREG)
    start = time.monotonic()
    with pytest.raises(ParseError):
        parse_message(raw)
    assert time.monotonic() - start < 2.0


def test_huge_single_line_body_is_fast() -> None:
    raw = build_mail("[PATCH 6.1.y] x", "commit " + "a" * 2_000_000 + "\n")
    start = time.monotonic()
    rec = parse_message(raw)
    assert time.monotonic() - start < 2.0
    assert isinstance(rec, BackportPost)
    assert rec.upstreams == ()


@pytest.mark.parametrize(
    "header",
    [
        "Subject: =?x-unknown-charset?b?Zm9v?=",
        "Subject: =?utf-8?b?!!!not-base64!!!?=",
        "Subject: =?utf-8?q?=FF=FE=00?=",
        "From: =?utf-8?b?" + "A" * 5000 + "?= <x@y>",
        "Message-ID: <a\x00b@c>",
        "In-Reply-To: <" + "<" * 5000,
    ],
    ids=["unknown-charset", "bad-base64", "bad-utf8", "huge-encoded-word", "nul", "angle-flood"],
)
def test_weird_headers_do_not_crash(header: str) -> None:
    headers = {
        "From": "From: a <a@b>",
        "Subject": "Subject: [PATCH 6.1.y] x",
        "Message-ID": "Message-ID: <ok@b>",
    }
    headers[header.split(":", 1)[0]] = header
    raw = ("\n".join(headers.values()) + "\n\nbody\n").encode("utf-8")
    try:
        rec = parse_message(raw)
    except ParseError:
        return  # rejected loudly: fine
    assert isinstance(rec, BackportPost | OtherMail | FailedRequest | ReviewMail)


def test_nul_and_binary_body_do_not_crash() -> None:
    raw = build_mail("[PATCH 6.1.y] x", "start\n") + b"\x00\xff\xfe" * 1000
    rec = parse_message(raw)
    assert isinstance(rec, BackportPost)


def test_message_id_with_control_chars_is_dropped() -> None:
    raw = build_mail("[PATCH 6.1.y] x", "", message_id="<a\x1b[31mb@c>")
    rec = parse_message(raw)
    assert rec.envelope.message_id is None


# --- terminal injection --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\x1b[31mred\x1b[0m", "red"),
        ("a\x1b]0;title\x07b", "ab"),
        ("bell\x07", "bell"),
        ("tab\tok", "tab ok"),
        ("new\nline", "new line"),
        ("c1\x9bcontrol", "c1control"),
        ("unicode ok: é中", "unicode ok: é中"),
    ],
    ids=["csi", "osc", "bel", "tab", "newline", "c1", "unicode"],
)
def test_for_terminal_strips_controls(text: str, expected: str) -> None:
    assert for_terminal(text) == expected


# --- rule 7: SQLite uses parameters only ----------------------------------------


@pytest.mark.parametrize(
    "mid",
    # Message-IDs cannot contain spaces (they would be rejected before the
    # store), so SQL comments stand in for them.
    [
        "x');DROP/**/TABLE/**/failed;--@gregkh",
        'x";DELETE/**/FROM/**/failed;--@gregkh',
        "x'/**/OR/**/'1'='1@gregkh",
    ],
    ids=["drop", "delete", "or-true"],
)
def test_sql_injection_strings_round_trip(
    mid: str, make_inbox: InboxFactory, tmp_path: Path
) -> None:
    result = mirror.collect(make_inbox([failed_mail(message_id=mid)]), max_count=10)
    db = tmp_path / "state.sqlite3"
    with store.open_store(db) as conn:
        store.save_collected(conn, result)
    with store.open_store(db) as conn:
        loaded = store.load_collected(conn)
    assert [f.envelope.message_id for f in loaded.failed] == [mid]
    with contextlib.closing(sqlite3.connect(db)) as raw:
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "failed" in tables
