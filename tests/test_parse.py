# SPDX-License-Identifier: GPL-2.0-or-later
"""Unit tests for classifying and parsing stable list messages."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from stable_backlog.model import BackportPost, FailedRequest, OtherMail, ReviewMail
from stable_backlog.parse import ParseError, parse_message
from tests.mailfactory import GREG, build_mail, failed_body, failed_mail, golden_mail

TTY = "b48820de8c1a596c5b99775524dfce73c6576c5d"
PMD = "fe6cf984939d8e12cb33a99673c8d026c5135e68"


# --- FAILED requests -------------------------------------------------------


def test_failed_from_lore() -> None:
    rec = parse_message(golden_mail("tty-failed-6.6"))
    assert isinstance(rec, FailedRequest)
    assert rec.upstream == TTY
    assert rec.tree == "6.6"
    assert rec.envelope.message_id == "2026100550-nest-aviator-8a3f@gregkh"
    assert rec.envelope.sender == "gregkh@linuxfoundation.org"
    assert rec.envelope.date == datetime(2026, 10, 5, 8, 50, 50, tzinfo=timezone.utc)
    assert rec.title == "tty: port: fix tty_port_tty_vhangup() race"


def test_failed_title_comes_from_body_when_subject_is_truncated() -> None:
    # Greg's subject cuts long titles ("... to the folio on"); the original
    # commit's Subject in the body has the whole title.
    rec = parse_message(golden_mail("pmd-failed-6.12"))
    assert isinstance(rec, FailedRequest)
    assert rec.upstream == PMD
    assert rec.title == "mm/huge_memory: transfer the pmd dirty bit to the folio on zap"


def test_failed_title_with_quotes() -> None:
    title = 'xfs: add "is cow staging" helpers in scrub'
    rec = parse_message(failed_mail(title=title, tree="6.1"))
    assert isinstance(rec, FailedRequest)
    assert rec.tree == "6.1"
    assert rec.title == title


def test_failed_rfc2047_encoded_subject() -> None:
    subject = (
        "=?UTF-8?q?FAILED=3A_patch_=22=5BPATCH=5D_tty=3A_port=3A_fix_"
        "tty=5Fport=5Ftty=5Fvhangup=28=29_race=22_failed_to_apply_to_"
        "6=2E6-stable_tree?="
    )
    rec = parse_message(failed_mail(subject=subject))
    assert isinstance(rec, FailedRequest)
    assert rec.tree == "6.6"


def test_failed_without_cherry_pick_line_is_an_error() -> None:
    body = failed_body(TTY, "6.6", "x@gregkh", "t").replace("git cherry-pick -x", "git cp")
    with pytest.raises(ParseError, match="cherry-pick"):
        parse_message(failed_mail(body=body))


def test_failed_with_two_cherry_pick_lines_is_an_error() -> None:
    body = failed_body(TTY, "6.6", "x@gregkh", "t")
    body += f"git cherry-pick -x {PMD}\n"
    with pytest.raises(ParseError, match="cherry-pick"):
        parse_message(failed_mail(body=body))


def test_failed_tree_mismatch_between_subject_and_body_is_an_error() -> None:
    body = failed_body(TTY, "6.1", "x@gregkh", "t")
    with pytest.raises(ParseError, match="tree"):
        parse_message(failed_mail(tree="6.6", body=body))


def test_failed_like_subject_not_understood_is_an_error() -> None:
    with pytest.raises(ParseError, match="subject"):
        parse_message(failed_mail(subject='FAILED: patch "[PATCH] x" did not apply'))


def test_failed_without_message_id_is_an_error() -> None:
    raw = build_mail(
        'FAILED: patch "[PATCH] t" failed to apply to 6.6-stable tree',
        failed_body(TTY, "6.6", "x@gregkh", "t"),
        sender=GREG,
        message_id=None,
    )
    with pytest.raises(ParseError, match="Message-ID"):
        parse_message(raw)


def test_failed_subject_from_unknown_sender_is_not_a_request() -> None:
    # Anyone can mail stable@; only the stable maintainers send FAILED reports.
    rec = parse_message(failed_mail(sender="Mallory <mallory@example.org>"))
    assert isinstance(rec, OtherMail)
    assert rec.note == "failed-subject-from-unknown-sender"


# --- backports -------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "trees", "upstream"),
    [
        ("gso-backport-6.1", ("6.1",), "006c8b834aed8b02bd8fb9fb05e9e7ead37d1763"),
        ("acpi-series-6.18-2", ("6.18",), "f234fdaae1cad8c39265e7ca0a14633076ec7154"),
        ("damon-backport-6.18-1", ("6.18",), "90179da203ba8b708c84a12a07cd44be0f346334"),
        ("pmd-backport-6.18-sasha", ("6.18",), PMD),
        ("tty-backport-5.15-2", ("5.15",), TTY),
    ],
    ids=["bracket-form", "commit-upstream-form", "cherry-picked-form", "by-sasha", "series-2of2"],
)
def test_backport_from_lore(fixture: str, trees: tuple[str, ...], upstream: str) -> None:
    rec = parse_message(golden_mail(fixture))
    assert isinstance(rec, BackportPost)
    assert rec.trees == trees
    assert rec.upstreams == (upstream,)


def test_backport_threading_headers() -> None:
    rec = parse_message(golden_mail("acpi-series-6.18-2"))
    assert isinstance(rec, BackportPost)
    assert rec.envelope.in_reply_to == "20261024105710.593-1-ue081723@gmail.com"
    assert rec.envelope.references == (
        "2026102308-twisting-bazooka-12b5@gregkh",
        "20261024105710.593-1-ue081723@gmail.com",
    )


def test_cover_letter_is_a_backport_post_without_hashes() -> None:
    rec = parse_message(golden_mail("acpi-series-6.18-0"))
    assert isinstance(rec, BackportPost)
    assert rec.trees == ("6.18",)
    assert rec.upstreams == ()


@pytest.mark.parametrize(
    ("subject", "trees"),
    [
        ("[PATCH 6.1.y] x: fix y", ("6.1",)),
        ("[PATCH 6.1] x: fix y", ("6.1",)),
        ("[PATCH 5.15.y 1/2] x: fix y", ("5.15",)),
        ("[PATCH v2 6.6.y] x: fix y", ("6.6",)),
        ("[PATCH 6.1.y 6.6.y] x: fix y", ("6.1", "6.6")),
        ("[PATCH AUTOSEL 6.12 3/9] x: fix y", ("6.12",)),
        ("[PATCH 6.1 1/2] x: fix y", ("6.1",)),
    ],
    ids=["dot-y", "plain", "series", "v2", "two-trees", "autosel", "contributor-n-of-m"],
)
def test_backport_subject_variants(subject: str, trees: tuple[str, ...]) -> None:
    body = f"[ Upstream commit {TTY} ]\n\nfix\n"
    rec = parse_message(build_mail(subject, body))
    assert isinstance(rec, BackportPost)
    assert rec.trees == trees


@pytest.mark.parametrize(
    "subject",
    [
        "[PATCH net v6 1/1] net: gso: limit recursive IP-in-IP segmentation",
        "[PATCH] mm/huge_memory: transfer the pmd dirty bit",
        "Re: [PATCH 6.1.y] tty: port: fix tty_port_tty_vhangup() race",
        "[PATCH 6.1.190] x: version, not a tree",
    ],
    ids=["mainline-series", "mainline-patch", "reply", "release-number"],
)
def test_not_a_backport(subject: str) -> None:
    rec = parse_message(build_mail(subject, f"commit {TTY} upstream.\n"))
    assert isinstance(rec, OtherMail)


def test_quoted_marker_is_ignored() -> None:
    body = f"> [ Upstream commit {TTY} ]\n> commit {PMD} upstream.\n"
    rec = parse_message(build_mail("[PATCH 6.1.y] x: fix y", body))
    assert isinstance(rec, BackportPost)
    assert rec.upstreams == ()


def test_short_upstream_hash_is_kept() -> None:
    rec = parse_message(build_mail("[PATCH 6.1.y] x", "[ Upstream commit b48820de8c1a ]\n"))
    assert isinstance(rec, BackportPost)
    assert rec.upstreams == ("b48820de8c1a",)


def test_backport_without_message_id_is_kept() -> None:
    rec = parse_message(
        build_mail("[PATCH 6.1.y] x", f"commit {TTY} upstream.\n", message_id=None)
    )
    assert isinstance(rec, BackportPost)
    assert rec.envelope.message_id is None


# --- stable review series, replies ------------------------------------------


def test_stable_review_mail_is_not_a_backport() -> None:
    rec = parse_message(golden_mail("tty-review-6.6-greg"))
    assert isinstance(rec, ReviewMail)
    assert rec.tree == "6.6"


def test_reply_quoting_a_failed_mail_is_other() -> None:
    rec = parse_message(golden_mail("damon-reply-6.18-sj"))
    assert isinstance(rec, OtherMail)
    assert rec.envelope.in_reply_to == "2026092900-avenue-magnetize-e7a6@gregkh"
    assert rec.envelope.references == ()


def test_sasha_queued_reply_is_other() -> None:
    rec = parse_message(golden_mail("tty-reply-6.1-sasha"))
    assert isinstance(rec, OtherMail)
    assert rec.envelope.sender == "sashal@kernel.org"


def test_naive_date_is_treated_as_utc() -> None:
    raw = build_mail("[PATCH 6.1.y] x", "", date="Sat, 24 Oct 2026 12:00:00 -0000")
    rec = parse_message(raw)
    assert rec.envelope.date == datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def test_bad_date_becomes_none() -> None:
    rec = parse_message(build_mail("[PATCH 6.1.y] x", "", date="not a date"))
    assert rec.envelope.date is None
