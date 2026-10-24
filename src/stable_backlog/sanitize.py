# SPDX-License-Identifier: GPL-2.0-or-later
"""Make untrusted text safe to print on a terminal.

Subjects and sender names come from anyone who mails stable@; printed raw,
escape sequences in them could rewrite the terminal (title, colours, cursor).
"""

from __future__ import annotations

import re

# ESC-based sequences: CSI (ESC [ ... final), OSC (ESC ] ... BEL or ESC \),
# and two-character escapes (ESC + one byte).
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]{0,64}[ -/]{0,16}[@-~]"
    r"|\x1b\][^\x07\x1b]{0,4096}(?:\x07|\x1b\\)?"
    r"|\x1b[@-_]?"
)
_WHITESPACE_RE = re.compile(r"[\t\n\r\v\f]")
# Remaining C0 controls, DEL and C1 controls (U+0080..U+009F).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def for_terminal(text: str) -> str:
    """Strip escape sequences and control characters; tabs/newlines -> space."""
    text = _ANSI_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return _CONTROL_RE.sub("", text)
