"""
Single-instance enforcement for the QuantCore Control Panel.

Uses a lockfile under the OS temp dir containing the owning PID and an ISO
timestamp. A second launch reads the lockfile and:
  * refuses (returns False) iff the recorded PID is still alive
  * reclaims (returns True) iff the PID is dead, missing, or unparseable

Per PRD FR-T10, IO failures during write are non-fatal: the launch proceeds
without single-instance protection rather than blocking the user.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import psutil

log = logging.getLogger(__name__)


def _read_lockfile_pid(lockfile_path: Path) -> int | None:
    """Return the PID stored in `lockfile_path`, or None if missing /
    unreadable / unparseable. Never raises."""
    try:
        text = lockfile_path.read_text(encoding="utf-8")
    except OSError:
        return None
    first = text.splitlines()[0] if text else ""
    try:
        return int(first.strip())
    except ValueError:
        return None


def acquire_lock(lockfile_path: Path) -> bool:
    """Attempt to claim `lockfile_path` for the current process.

    Returns True when this process is now the recognised owner — either
    because no live owner existed, or because writing the lockfile failed
    and we're proceeding without protection (PRD FR-T10). Returns False
    only when another live PID currently holds the lockfile.
    """
    existing_pid = _read_lockfile_pid(lockfile_path)
    if existing_pid is not None and psutil.pid_exists(existing_pid):
        return False

    payload = f"{os.getpid()}\n{datetime.now().isoformat(timespec='seconds')}"
    try:
        lockfile_path.write_text(payload, encoding="utf-8")
    except OSError as e:
        log.warning(f"single_instance: lockfile write failed ({e!r}) — "
                    f"continuing without single-instance protection")
        return True

    return True


def release_lock(lockfile_path: Path) -> None:
    """Best-effort delete of the lockfile. Silent on missing-file or IO
    errors — the goal here is hygiene at shutdown, not correctness."""
    try:
        lockfile_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning(f"single_instance: lockfile delete failed ({e!r})")
