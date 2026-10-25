# SPDX-License-Identifier: GPL-2.0-or-later
"""Helpers to load golden fixtures and to build synthetic test messages."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden"

GREG = "<gregkh@linuxfoundation.org>"


def golden_mail(name: str) -> bytes:
    return (GOLDEN / "mail" / f"{name}.eml").read_bytes()


def build_mail(
    subject: str,
    body: str,
    *,
    sender: str = "Some One <someone@example.org>",
    message_id: str | None = "<test-1@example.org>",
    in_reply_to: str | None = None,
    references: str | None = None,
    date: str = "Sat, 24 Oct 2026 12:00:00 +0000",
    extra: str = "",
) -> bytes:
    """Build a raw RFC 5322 message. Header values are used verbatim."""
    lines = [f"From: {sender}", f"Subject: {subject}", f"Date: {date}"]
    if message_id is not None:
        lines.append(f"Message-ID: {message_id}")
    if in_reply_to is not None:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references is not None:
        lines.append(f"References: {references}")
    lines += ["MIME-Version: 1.0", "Content-Type: text/plain; charset=UTF-8"]
    lines.append("Content-Transfer-Encoding: 8bit")
    if extra:
        lines.append(extra.rstrip("\n"))
    return ("\n".join(lines) + "\n\n" + body).encode("utf-8")


def failed_body(
    upstream: str,
    tree: str,
    message_id: str,
    title: str,
    *,
    author: str = "Some Author <author@example.org>",
    files: Sequence[str] = (),
) -> str:
    """A FAILED body in the format stable-queue's tooling sends (trimmed).

    The quoted original commit gets `From: <author>` and one `diff --git`
    line per file.
    """
    diffs = "".join(f"diff --git a/{f} b/{f}\n" for f in files)
    return f"""
The patch below does not apply to the {tree}-stable tree.
If someone wants it applied there, or to any other stable or longterm
tree, then please email the backport, including the original git commit
id to <stable@vger.kernel.org>.

To reproduce the conflict and resubmit, you may use the following commands:

git fetch https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/ linux-{tree}.y
git checkout FETCH_HEAD
git cherry-pick -x {upstream}
# <resolve conflicts, build, test, etc.>
git commit -s
git send-email --to '<stable@vger.kernel.org>' --in-reply-to '{message_id}' --subject-prefix 'PATCH {tree}.y' 'HEAD^..'

Possible dependencies:



thanks,

greg k-h

------------------ original commit in Linus's tree ------------------

From {upstream} Mon Sep 17 00:00:00 2001
From: {author}
Date: Thu, 10 Sep 2026 14:51:14 +0200
Subject: [PATCH] {title}

The commit message.

{diffs}"""


def failed_mail(
    upstream: str = "b48820de8c1a596c5b99775524dfce73c6576c5d",
    tree: str = "6.6",
    message_id: str = "2026100550-test-failed@gregkh",
    title: str = "tty: port: fix tty_port_tty_vhangup() race",
    *,
    subject: str | None = None,
    body: str | None = None,
    sender: str = GREG,
    author: str = "Some Author <author@example.org>",
    files: Sequence[str] = (),
    date: str = "Sat, 24 Oct 2026 12:00:00 +0000",
) -> bytes:
    subj = subject or f'FAILED: patch "[PATCH] {title}" failed to apply to {tree}-stable tree'
    if body is None:
        body = failed_body(upstream, tree, message_id, title, author=author, files=files)
    return build_mail(subj, body, sender=sender, message_id=f"<{message_id}>", date=date)
