# SPDX-License-Identifier: GPL-2.0-or-later
"""The only place that runs git.

Rules (docs/DESIGN.md, "Security model"):
- argument lists only, never a shell;
- only fixed git subcommands, chosen by this package, never by message data;
- values from messages (commit hashes) are validated before use;
- no network on read: other protocols are disabled and lazy fetch is off;
- the network is used only by network_run(), only for the fixed https
  sources in validate_url().
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
NETWORK_TIMEOUT = 1800  # seconds, for clone / fetch

# The only sources ever contacted (docs/DESIGN.md, "Data sources").
LORE_URL_RE = re.compile(r"https://lore\.kernel\.org/stable/git/(?:0|[1-9][0-9]{0,2})\.git")
STABLE_QUEUE_URL = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/stable-queue.git"

# Only https may ever be used (and only by an explicit fetch).
_SAFE_CONFIG = ("-c", "protocol.allow=never", "-c", "protocol.https.allow=always")
# Added for commands that use the network.
_NETWORK_CONFIG = (
    *("-c", "credential.helper="),  # never ask a credential helper; kernel.org needs none
    *("-c", "http.followRedirects=false"),  # never be sent to another host
    *("-c", "transfer.fsckObjects=true"),  # reject malformed objects
)


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


def validate_url(url: str) -> str:
    """Return url if it is a lore stable epoch or stable-queue; nothing else is fetched."""
    if not isinstance(url, str) or not (url == STABLE_QUEUE_URL or LORE_URL_RE.fullmatch(url)):
        raise ValueError(f"not an allowed source: {url!r:.80}")
    return url


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


def network_command(
    cwd: Path, args: Sequence[str], url: str, rest: Sequence[str] = ()
) -> list[str]:
    """Build the argv for `git -C cwd <args> -- <url> <rest>` with the network
    settings; url must be one of the fixed sources."""
    url = validate_url(url)
    return [
        git_executable(),
        *_SAFE_CONFIG,
        *_NETWORK_CONFIG,
        "-C",
        str(cwd),
        *args,
        "--",
        url,
        *rest,
    ]


def _execute(argv: list[str], what: str, *, stdin: bytes | None, timeout: int) -> bytes:
    try:
        out = subprocess.run(  # noqa: S603 - argv built by command() / network_command()
            argv,
            input=stdin,
            capture_output=True,
            check=True,
            timeout=timeout,
            env=_env(),
            shell=False,
        )
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"{what} failed: {err}") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"{what} timed out") from e
    except OSError as e:
        raise GitError(f"cannot run {what}: {e}") from e
    return out.stdout


def run(repo: Path, args: Sequence[str], *, stdin: bytes | None = None) -> bytes:
    """Run git and return stdout; raise GitError with context on failure."""
    what = f"git {' '.join(args[:3])} in {repo}"
    return _execute(command(repo, args), what, stdin=stdin, timeout=GIT_TIMEOUT)


def network_run(cwd: Path, args: Sequence[str], url: str, rest: Sequence[str] = ()) -> bytes:
    """Run a git command that contacts url (clone, fetch, ls-remote)."""
    argv = network_command(cwd, args, url, rest)
    what = f"git {' '.join(args[:1])} {url} in {cwd}"
    return _execute(argv, what, stdin=None, timeout=NETWORK_TIMEOUT)


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """Whether ancestor is reachable from descendant (`git merge-base --is-ancestor`)."""
    args = ["merge-base", "--is-ancestor", validate_full_hash(ancestor)]
    argv = command(repo, [*args, validate_full_hash(descendant)])
    try:
        # check=False: exit status 1 is the answer "no", not a failure.
        out = subprocess.run(  # noqa: S603 - argv built by command()
            argv, capture_output=True, check=False, timeout=GIT_TIMEOUT, env=_env(), shell=False
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise GitError(f"git merge-base in {repo}: {e}") from e
    if out.returncode not in (0, 1):
        err = out.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git merge-base --is-ancestor failed in {repo}: {err}")
    return out.returncode == 0


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
