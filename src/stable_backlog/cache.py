# SPDX-License-Identifier: GPL-2.0-or-later
"""The cache directory (mirrors and snapshot) and the lock that serialises syncs.

Later commands read the git repositories kept here, so nobody but the owner
may write to the directory. An existing directory that is a symlink, belongs
to someone else or is writable by group or others is refused, never "fixed".
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

APP_DIR = "stable-backlog"
LOCK_FILE = "lock"


class CacheError(RuntimeError):
    """The cache directory is unsafe to use, or another sync holds the lock."""


def default_dir(env: Mapping[str, str]) -> Path:
    """$XDG_CACHE_HOME/stable-backlog, else ~/.cache/stable-backlog.

    An empty or relative XDG_CACHE_HOME is ignored, as the XDG spec says.
    """
    xdg = env.get("XDG_CACHE_HOME", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / APP_DIR
    home = env.get("HOME", "")
    if not home or not os.path.isabs(home):
        raise CacheError("HOME is not an absolute path; give the cache directory explicitly")
    return Path(home) / ".cache" / APP_DIR


def prepare(path: Path) -> Path:
    """Create path (mode 0700) if needed, check that it is safe; return it absolute."""
    path = Path(os.path.abspath(path))  # not resolve(): a symlink must stay visible
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            path.mkdir(mode=0o700)
        st = os.lstat(path)
    except OSError as e:
        raise CacheError(f"cannot create the cache directory {path}: {e}") from e
    if stat.S_ISLNK(st.st_mode):
        raise CacheError(f"{path}: is a symlink; give the real directory")
    if not stat.S_ISDIR(st.st_mode):
        raise CacheError(f"{path}: not a directory")
    if st.st_uid != os.getuid():
        raise CacheError(f"{path}: owned by uid {st.st_uid}, not by you")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        mode = stat.S_IMODE(st.st_mode)
        raise CacheError(f"{path}: writable by group or others (mode {mode:04o}); chmod 700 it")
    return path


@contextmanager
def lock(directory: Path) -> Iterator[None]:
    """Hold an exclusive lock on directory/lock; fail at once if it is taken."""
    path = directory / LOCK_FILE
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise CacheError(f"cannot open the lock file {path}: {e}") from e
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise CacheError(f"another sync is running (it holds {path})") from e
        yield
    finally:
        os.close(fd)  # also releases the lock
