# SPDX-License-Identifier: GPL-2.0-or-later
"""Classify and parse stable list messages.

Every message is untrusted input. This module only reads text: nothing from a
message is executed, applied, or used as a file name or command argument.
Anything that looks like a FAILED report but cannot be understood raises
ParseError, so the caller can count it instead of silently dropping it.
"""

from __future__ import annotations

import email
import email.errors
import email.message
import email.policy
import email.utils
import re
from datetime import datetime, timezone

from stable_backlog.model import (
    BackportPost,
    Envelope,
    FailedRequest,
    OtherMail,
    Parsed,
    ReviewMail,
)

MAX_MESSAGE_BYTES = 5 * 1024 * 1024
MAX_SUBJECT_CHARS = 1000  # longer subjects are cut before any regex
MAX_HEADER_CHARS = 20_000  # References and friends
MAX_BODY_CHARS = 2_000_000
MAX_MESSAGE_ID_CHARS = 512

# Only the stable maintainers send FAILED reports and -rc review series.
STABLE_MAINTAINERS = frozenset({"gregkh@linuxfoundation.org", "sashal@kernel.org"})

FAILED_PREFIX = "FAILED: patch "
FAILED_SUBJECT_RE = re.compile(
    r'FAILED: patch "(?P<title>.*)" failed to apply to (?P<tree>\d{1,2}\.\d{1,3})-stable tree'
)
CHERRY_PICK_RE = re.compile(r"^git cherry-pick -x (?P<hash>[0-9a-f]{40})[ \t]*$", re.M)
CHERRY_PICK_ANY_RE = re.compile(r"^git cherry-pick ", re.M)
SUBJECT_PREFIX_RE = re.compile(r"--subject-prefix 'PATCH (?P<tree>\d{1,2}\.\d{1,3})\.y'")
ORIGINAL_COMMIT_SEPARATOR = "------------------ original commit in Linus's tree ------------------"
ORIGINAL_SUBJECT_RE = re.compile(
    r"^Subject: (?:\[PATCH\] )?(?P<title>[^\n]{0,1000}(?:\n[ \t][^\n]{0,1000}){0,5})", re.M
)
ORIGINAL_HEADER_WINDOW = 4000  # chars after the separator that hold the commit header

PATCH_SUBJECT_RE = re.compile(r"\[PATCH (?P<inner>[^\]]{1,120})\][ \t]*(?P<title>.*)")
TREE_TOKEN_RE = re.compile(r"(?<![\w.])(?P<tree>\d{1,2}\.\d{1,3})(?:\.y)?(?![\w.])")
REVIEW_INNER_RE = re.compile(r"\d{1,2}\.\d{1,3} \d{1,5}/\d{1,5}")
UPSTREAM_RES = (
    re.compile(r"^\[ ?Upstream commit (?P<hash>[0-9a-f]{12,40}) ?\]", re.M),
    re.compile(r"^commit (?P<hash>[0-9a-f]{12,40}) upstream\.?[ \t]*$", re.M),
    re.compile(r"^\(cherry picked from commit (?P<hash>[0-9a-f]{12,40})\)", re.M),
)
MSGID_RE = re.compile(r"<(?P<id>[^<>\s]{1,512})>")
VALID_MSGID_RE = re.compile(r"[!-~]{1,512}")  # printable ASCII, no spaces

_POLICY = email.policy.default
# Exceptions the email package is known to raise on malformed input.
_EMAIL_ERRORS = (
    email.errors.MessageError,
    ValueError,
    TypeError,
    IndexError,
    AttributeError,
    LookupError,
)


class ParseError(ValueError):
    """A message looks like something we track but could not be understood."""

    def __init__(self, reason: str, message_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message_id = message_id


class MessageTooLarge(ParseError):
    """A message is larger than MAX_MESSAGE_BYTES."""


def parse_message(raw: bytes) -> Parsed:
    """Classify one raw RFC 5322 message and extract what we track."""
    if len(raw) > MAX_MESSAGE_BYTES:
        raise MessageTooLarge("too-large")
    msg = email.message_from_bytes(raw, policy=_POLICY)
    env = _envelope(msg)
    subject = env.subject
    if subject.startswith(FAILED_PREFIX):
        if env.sender not in STABLE_MAINTAINERS:
            return OtherMail(env, note="failed-subject-from-unknown-sender")
        return _failed(msg, env)
    m = PATCH_SUBJECT_RE.match(subject)
    if m is None:
        return OtherMail(env)
    trees = _trees(m.group("inner"))
    if not trees:
        return OtherMail(env)
    upstreams = _upstreams(_body_text(msg))
    if env.sender in STABLE_MAINTAINERS and REVIEW_INNER_RE.fullmatch(m.group("inner")):
        return ReviewMail(env, trees[0], upstreams)
    return BackportPost(env, trees, upstreams, m.group("title").strip())


def normalize_title(title: str) -> str:
    """Comparable form of a commit title (for the low-confidence fallback)."""
    return " ".join(title.split()).casefold().rstrip(".")


# --- envelope -----------------------------------------------------------------


def _envelope(msg: email.message.EmailMessage) -> Envelope:
    # Message-IDs need no decoding; the structured parser would also rewrite
    # "(...)" inside them as RFC 5322 comments, so read the raw values.
    message_id = _single_id(_raw_header(msg, "Message-ID"))
    try:
        return Envelope(
            message_id=message_id,
            subject=" ".join(_header(msg, "Subject")[:MAX_SUBJECT_CHARS].split()),
            sender=_sender(_header(msg, "From")),
            date=_date(_header(msg, "Date")),
            in_reply_to=_single_id(_raw_header(msg, "In-Reply-To")),
            references=_ids(_raw_header(msg, "References")),
        )
    except _EMAIL_ERRORS as e:
        raise ParseError(f"malformed header: {type(e).__name__}", message_id) from e


def _raw_header(msg: email.message.EmailMessage, name: str) -> str:
    """First undecoded value of a header ("" if absent)."""
    for key, value in msg.raw_items():
        if key.lower() == name.lower():
            return str(value)[:MAX_HEADER_CHARS]
    return ""


def _header(msg: email.message.EmailMessage, name: str) -> str:
    """Decoded header value ("" if absent); falls back to the raw value."""
    try:
        value = msg.get(name)
        text = "" if value is None else str(value)
    except _EMAIL_ERRORS:
        # The structured parser choked; use the undecoded value instead.
        text = _raw_header(msg, name)
    return text[:MAX_HEADER_CHARS]


def _single_id(value: str) -> str | None:
    m = MSGID_RE.search(value)
    if m is None or not VALID_MSGID_RE.fullmatch(m.group("id")):
        return None
    return m.group("id")


def _ids(value: str) -> tuple[str, ...]:
    out = []
    for m in MSGID_RE.finditer(value):
        if VALID_MSGID_RE.fullmatch(m.group("id")) and m.group("id") not in out:
            out.append(m.group("id"))
    return tuple(out)


def _sender(value: str) -> str:
    _name, addr = email.utils.parseaddr(value)
    return addr.strip().lower()[:320]


def _date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:  # "-0000": unknown zone, treat as UTC
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


# --- body ---------------------------------------------------------------------


def _body_text(msg: email.message.EmailMessage) -> str:
    """The text/plain body as str, decoding as leniently as needed."""
    try:
        part = msg.get_body(preferencelist=("plain",))
    except _EMAIL_ERRORS:
        part = None
    if part is None:
        return ""
    try:
        content = part.get_content()
    except _EMAIL_ERRORS:
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
    if not isinstance(content, str):
        return ""
    return content[:MAX_BODY_CHARS]


def find_upstreams(text: str) -> tuple[str, ...]:
    """Upstream commit hashes named by marker lines in a patch or message.

    Only whole marker lines count ("[ Upstream commit X ]", "commit X
    upstream.", "(cherry picked from commit X)"); quoted lines, Fixes: tags
    and hashes in prose do not.
    """
    return _upstreams(text[:MAX_BODY_CHARS])


def _upstreams(body: str) -> tuple[str, ...]:
    found: list[str] = []
    for regex in UPSTREAM_RES:
        for m in regex.finditer(body):
            if m.group("hash") not in found:
                found.append(m.group("hash"))
    return tuple(found)


def _trees(inner: str) -> tuple[str, ...]:
    out: list[str] = []
    for m in TREE_TOKEN_RE.finditer(inner):
        if m.group("tree") not in out:
            out.append(m.group("tree"))
    return tuple(out)


# --- FAILED reports -----------------------------------------------------------


def _failed(msg: email.message.EmailMessage, env: Envelope) -> FailedRequest:
    mid = env.message_id
    m = FAILED_SUBJECT_RE.fullmatch(env.subject)
    if m is None:
        raise ParseError("FAILED subject not understood", mid)
    if mid is None:
        raise ParseError("FAILED report without a valid Message-ID")
    tree = m.group("tree")
    body = _body_text(msg)
    picks = CHERRY_PICK_RE.findall(body)
    if len(picks) != 1 or len(CHERRY_PICK_ANY_RE.findall(body)) != 1:
        raise ParseError(f"expected one 'git cherry-pick -x <hash>' line, got {len(picks)}", mid)
    prefix = SUBJECT_PREFIX_RE.search(body)
    if prefix is not None and prefix.group("tree") != tree:
        raise ParseError(f"tree {tree} in subject but {prefix.group('tree')} in body", mid)
    return FailedRequest(env, picks[0], tree, _failed_title(body, m.group("title")))


def _failed_title(body: str, subject_title: str) -> str:
    """Full title from the original commit's header; the subject may be cut."""
    start = body.find(ORIGINAL_COMMIT_SEPARATOR)
    if start >= 0:
        m = ORIGINAL_SUBJECT_RE.search(body, start, start + ORIGINAL_HEADER_WINDOW)
        if m is not None:
            return " ".join(m.group("title").split())
    return subject_title.removeprefix("[PATCH] ").strip()
