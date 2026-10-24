# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the cache directory and the sync lock."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from stable_backlog import cache


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"HOME": "/home/u"}, "/home/u/.cache/stable-backlog"),
        ({"HOME": "/home/u", "XDG_CACHE_HOME": "/x/cache"}, "/x/cache/stable-backlog"),
        ({"HOME": "/home/u", "XDG_CACHE_HOME": "rel/cache"}, "/home/u/.cache/stable-backlog"),
        ({"HOME": "/home/u", "XDG_CACHE_HOME": ""}, "/home/u/.cache/stable-backlog"),
    ],
    ids=["home", "xdg", "relative-xdg-ignored", "empty-xdg-ignored"],
)
def test_default_dir(env: dict[str, str], expected: str) -> None:
    assert cache.default_dir(env) == Path(expected)


@pytest.mark.parametrize(
    "env", [{}, {"HOME": ""}, {"HOME": "relative"}], ids=["unset", "empty", "relative"]
)
def test_default_dir_needs_an_absolute_home(env: dict[str, str]) -> None:
    with pytest.raises(cache.CacheError, match="HOME"):
        cache.default_dir(env)


def test_prepare_creates_a_private_directory(tmp_path: Path) -> None:
    path = tmp_path / "parent" / "stable-backlog"
    assert cache.prepare(path) == path
    assert _mode(path) == 0o700


def test_prepare_makes_a_relative_path_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cache.prepare(Path("c")) == tmp_path / "c"


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755], ids=["0700", "0750", "0755"])
def test_prepare_accepts_a_directory_only_its_owner_can_write(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "c"
    path.mkdir()
    path.chmod(mode)
    assert cache.prepare(path) == path
    assert _mode(path) == mode  # never changed behind the user's back


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o702], ids=["0777", "0770", "0702"])
def test_prepare_refuses_a_directory_others_can_write(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "c"
    path.mkdir()
    path.chmod(mode)
    with pytest.raises(cache.CacheError, match="writable by group or others"):
        cache.prepare(path)


def test_prepare_refuses_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(cache.CacheError, match="symlink"):
        cache.prepare(link)


def test_prepare_refuses_a_file(tmp_path: Path) -> None:
    path = tmp_path / "c"
    path.write_text("")
    with pytest.raises(cache.CacheError, match="not a directory"):
        cache.prepare(path)


def test_prepare_reports_a_path_it_cannot_create(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("")
    with pytest.raises(cache.CacheError, match="cannot create the cache directory"):
        cache.prepare(tmp_path / "file" / "stable-backlog")


def test_prepare_refuses_a_directory_owned_by_someone_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "c"
    path.mkdir(mode=0o700)
    uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    with pytest.raises(cache.CacheError, match="owned by"):
        cache.prepare(path)


def test_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    with (
        cache.lock(tmp_path),
        pytest.raises(cache.CacheError, match="another sync"),
        cache.lock(tmp_path),
    ):
        pass
    with cache.lock(tmp_path):
        pass
    assert _mode(tmp_path / "lock") == 0o600


def test_lock_refuses_a_symlinked_lock_file(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").write_text("")
    (tmp_path / "lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(cache.CacheError, match="lock file"), cache.lock(tmp_path):
        pass
