"""
Unit tests for single_instance — lockfile-based duplicate-launch protection.

Covers:
  * acquire_lock: fresh / held by alive PID / held by dead PID / malformed / IO error
  * release_lock: present / absent / IO error

No real subprocess spawning — uses os.getpid() and a fake "definitely-dead" PID.
"""

from __future__ import annotations

import os
from pathlib import Path

import psutil
import pytest


# ── Helpers ────────────────────────────────────────────────────────

def _dead_pid() -> int:
    """Return a PID guaranteed not to be alive on this system.

    Strategy: scan upward from a high number until we find one psutil says
    is dead. Avoids the brittleness of "pick 999999 and pray"."""
    for candidate in range(900000, 999999):
        if not psutil.pid_exists(candidate):
            return candidate
    raise RuntimeError("could not find a dead PID")


# ── acquire_lock ────────────────────────────────────────────────────

def test_acquire_when_lockfile_missing_writes_pid_and_returns_true(tmp_path):
    from single_instance import acquire_lock

    lockfile = tmp_path / "app.lock"
    assert not lockfile.exists()

    result = acquire_lock(lockfile)

    assert result is True
    assert lockfile.exists()
    # First line is the PID.
    first_line = lockfile.read_text(encoding="utf-8").splitlines()[0]
    assert int(first_line) == os.getpid()


def test_acquire_when_lockfile_held_by_alive_pid_returns_false(tmp_path):
    """Second launch while the first is still running — must refuse."""
    from single_instance import acquire_lock

    lockfile = tmp_path / "app.lock"
    # Pretend the current process is "the first instance" — its PID is alive.
    lockfile.write_text(f"{os.getpid()}\n2026-05-04T22:00:00", encoding="utf-8")
    original = lockfile.read_text(encoding="utf-8")

    result = acquire_lock(lockfile)

    assert result is False
    # Lockfile must be untouched (we did not steal it).
    assert lockfile.read_text(encoding="utf-8") == original


def test_acquire_when_lockfile_holds_dead_pid_overwrites_and_returns_true(tmp_path):
    """Crashed previous instance left a stale lockfile — we should reclaim."""
    from single_instance import acquire_lock

    lockfile = tmp_path / "app.lock"
    dead = _dead_pid()
    lockfile.write_text(f"{dead}\n2026-04-01T10:00:00", encoding="utf-8")

    result = acquire_lock(lockfile)

    assert result is True
    first_line = lockfile.read_text(encoding="utf-8").splitlines()[0]
    assert int(first_line) == os.getpid()


def test_acquire_when_lockfile_malformed_treats_as_stale_returns_true(tmp_path):
    """Garbage content (truncated, non-numeric) shouldn't permanently block
    relaunch — treat as dead."""
    from single_instance import acquire_lock

    lockfile = tmp_path / "app.lock"
    lockfile.write_text("not a pid\n", encoding="utf-8")

    result = acquire_lock(lockfile)

    assert result is True
    first_line = lockfile.read_text(encoding="utf-8").splitlines()[0]
    assert int(first_line) == os.getpid()


def test_acquire_io_error_does_not_block_startup(tmp_path, monkeypatch):
    """Per PRD FR-T10: lockfile write failure must not stop the panel from
    launching — single-instance is best-effort, monitoring is not."""
    from single_instance import acquire_lock

    lockfile = tmp_path / "subdir" / "app.lock"  # parent doesn't exist
    # Force write to fail by patching Path.write_text to raise.
    real_write = Path.write_text
    def boom(self, *a, **k):
        if self == lockfile:
            raise OSError("disk full")
        return real_write(self, *a, **k)
    monkeypatch.setattr(Path, "write_text", boom)

    result = acquire_lock(lockfile)

    assert result is True


# ── release_lock ────────────────────────────────────────────────────

def test_release_existing_lockfile_deletes_it(tmp_path):
    from single_instance import release_lock

    lockfile = tmp_path / "app.lock"
    lockfile.write_text(f"{os.getpid()}", encoding="utf-8")
    assert lockfile.exists()

    release_lock(lockfile)

    assert not lockfile.exists()


def test_release_missing_lockfile_does_not_raise(tmp_path):
    from single_instance import release_lock

    lockfile = tmp_path / "never-was.lock"
    # Must not raise.
    release_lock(lockfile)


def test_release_io_error_does_not_raise(tmp_path, monkeypatch):
    from single_instance import release_lock

    lockfile = tmp_path / "app.lock"
    lockfile.write_text("123", encoding="utf-8")

    real_unlink = Path.unlink
    def boom(self, *a, **k):
        if self == lockfile:
            raise PermissionError("locked by AV")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", boom)

    # Must not raise.
    release_lock(lockfile)
