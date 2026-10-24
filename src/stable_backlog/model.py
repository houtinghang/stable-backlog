# SPDX-License-Identifier: GPL-2.0-or-later
"""Data types shared by the parser, the readers and the status logic.

All text fields hold data taken from untrusted messages or patch files; they
must be escaped or sanitised before display (see sanitize.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal


class Status(str, Enum):
    """Where one (upstream commit, stable tree) pair stands."""

    FAILED = "FAILED"  # FAILED report, no backport found (yet)
    BACKPORT_POSTED = "BACKPORT_POSTED"  # someone mailed a backport
    QUEUED = "QUEUED"  # in stable-queue queue-X.Y or staging-X.Y
    RELEASED = "RELEASED"  # in stable-queue releases/X.Y.Z


class Confidence(str, Enum):
    """How a backport or queue entry was matched to a FAILED request."""

    HASH = "hash"  # same upstream commit hash (reliable)
    TITLE = "title"  # same title only (fallback, low confidence)


@dataclass(frozen=True)
class Envelope:
    """Header fields every message has (as far as they could be parsed)."""

    message_id: str | None  # without angle brackets; None if missing/invalid
    subject: str  # decoded, whitespace collapsed
    sender: str  # lower-case address, or "" if unparseable
    date: datetime | None  # timezone-aware; None if missing/invalid
    in_reply_to: str | None
    references: tuple[str, ...]


@dataclass(frozen=True)
class FailedRequest:
    """A "FAILED: patch ... failed to apply to X.Y-stable tree" report."""

    envelope: Envelope
    upstream: str  # 40-hex upstream commit from the cherry-pick line
    tree: str  # "6.1"
    title: str  # title of the upstream commit


@dataclass(frozen=True)
class BackportPost:
    """A patch mailed for one or more stable trees ("[PATCH 6.1.y] ...")."""

    envelope: Envelope
    trees: tuple[str, ...]
    upstreams: tuple[str, ...]  # 12..40-hex hashes from upstream markers
    title: str


@dataclass(frozen=True)
class ReviewMail:
    """One patch of a stable maintainer's -rc review series."""

    envelope: Envelope
    tree: str
    upstreams: tuple[str, ...]


@dataclass(frozen=True)
class OtherMail:
    """Anything else: replies, mainline patches, discussions."""

    envelope: Envelope
    note: str | None = None  # why it was set aside, if notable


Parsed = FailedRequest | BackportPost | ReviewMail | OtherMail

Location = Literal["queue", "staging", "release"]


@dataclass(frozen=True)
class QueueEntry:
    """One stable-queue patch file that carries an upstream commit marker."""

    upstream: str  # 12..40 hex, as written in the patch
    tree: str  # "6.1"
    location: Location
    directory: str  # "queue-6.1", "staging-6.1" or "releases/6.1.190"
    path: str  # path inside stable-queue
    release: str | None  # "6.1.190" for releases, else None
