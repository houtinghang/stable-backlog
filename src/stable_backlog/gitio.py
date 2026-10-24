# SPDX-License-Identifier: GPL-2.0-or-later
"""The only place that runs git.

Rules (docs/DESIGN.md, "Security model"):
- argument lists only, never a shell;
- only fixed git subcommands, chosen by this package, never by message data;
- values from messages (commit hashes) are validated before use;
- no network on read: other protocols are disabled and lazy fetch is off.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

HASH_RE = re.compile(r"[0-9a-f]{12,40}")
FULL_HASH_RE = re.compile(r"[0-9a-f]{40}")
GIT_TIMEOUT = 300  # seconds, for non-streaming commands

# Only https may ever be used (and only by an explicit fetch).
_SAFE_CONFIG = ("-c", "protocol.allow=never", "-c", "protocol.https.allow=always")


class GitError(RuntimeError):
    """git failed; the message carries the command, repo and stderr."""


def validate_hash(value: str) -> str:
    """Return value if it is a 12..40 digit lower-case hex commit id."""
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise ValueError(f"not a commit hash: {value!r:.60}")
    return value


def validate_full_hash(value: str) -> str:
    """Return value if it is a full 40 digit lower-case hex object id."""
    if not isinstance(value, str) or not FULL_HASH_RE.fullmatch(value):
        raise ValueError(f"not a full commit hash: {value!r:.60}")
    return value


def git_executable() -> str:
    exe = shutil.which("git")
    if exe is None:
        raise GitError("git not found on PATH")
    return exe


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_NO_LAZY_FETCH": "1",  # never fetch missing objects implicitly
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
    )
    return env


def command(repo: Path, args: Sequence[str]) -> list[str]:
    """Build the argv for `git -C repo <args>`; args must be package constants
    or validated values."""
    return [git_executable(), *_SAFE_CONFIG, "-C", str(repo), *args]


def run(repo: Path, args: Sequence[str], *, stdin: bytes | None = None) -> bytes:
    """Run git and return stdout; raise GitError with context on failure."""
    argv = command(repo, args)
    try:
        out = subprocess.run(  # noqa: S603 - argv built by command()
            argv,
            input=stdin,
            capture_output=True,
            check=True,
            timeout=GIT_TIMEOUT,
            env=_env(),
            shell=False,
        )
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args[:3])} failed in {repo}: {err}") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args[:3])} timed out in {repo}") from e
    except OSError as e:
        raise GitError(f"cannot run git in {repo}: {e}") from e
    return out.stdout


def popen(repo: Path, args: Sequence[str]) -> subprocess.Popen[bytes]:
    """Start a streaming git process (for `cat-file --batch`)."""
    if not repo.exists():
        raise GitError(f"repository does not exist: {repo}")
    return subprocess.Popen(  # noqa: S603 - argv built by command()
        command(repo, args),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
        shell=False,
    )
