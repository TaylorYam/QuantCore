"""
Unit tests for GatewayMonitor.restart_account_thread — β recovery primitive
used by ROSA poller when a previous monitor thread has died.

We don't run real IB connections; the worker loop is monkey-patched to a
controllable stub so the test pins **only** the spawn/refuse semantics.
"""

from __future__ import annotations

import threading
import time

import pytest


# ── Test helpers ────────────────────────────────────────────────────

class _StubLoop:
    """Replacement for GatewayMonitor._monitor_loop.

    Per-name: blocks on its own Event. Tests release the event to let the
    thread exit. Records every invocation for assertions.
    """

    def __init__(self):
        self._gates: dict[str, threading.Event] = {}
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def gate_for(self, name: str) -> threading.Event:
        with self._lock:
            if name not in self._gates:
                self._gates[name] = threading.Event()
            return self._gates[name]

    def __call__(self, name: str) -> None:
        # _StubLoop has no __get__ → unbound when accessed via instance, so
        # signature is just (name,). The original _monitor_loop is bound.
        with self._lock:
            self.calls.append(name)
        self.gate_for(name).wait(timeout=5.0)


@pytest.fixture
def stub_loop(monkeypatch):
    from gateway_monitor import GatewayMonitor
    s = _StubLoop()
    monkeypatch.setattr(GatewayMonitor, "_monitor_loop", s)
    return s


def _new_monitor(names):
    from gateway_monitor import GatewayMonitor
    return GatewayMonitor(account_names=names)


def _wait_for_thread_alive(mon, name, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = mon._threads.get(name)
        if t is not None and t.is_alive():
            return t
        time.sleep(0.01)
    raise AssertionError(f"thread for {name} did not start within {timeout}s")


# ── restart_account_thread semantics ───────────────────────────────

def test_restart_spawns_when_never_started(stub_loop):
    mon = _new_monitor(["Rosa"])

    # Brand-new monitor — no thread for Rosa yet.
    assert mon._threads.get("Rosa") is None

    spawned = mon.restart_account_thread("Rosa")

    assert spawned is True
    t = _wait_for_thread_alive(mon, "Rosa")
    assert t.daemon is True

    # Cleanup: release the gate so the worker exits.
    stub_loop.gate_for("Rosa").set()
    t.join(timeout=2.0)


def test_restart_refuses_when_thread_alive(stub_loop):
    mon = _new_monitor(["Rosa"])
    mon.start()  # original start() spawns the thread

    t1 = _wait_for_thread_alive(mon, "Rosa")
    assert stub_loop.calls == ["Rosa"]

    # Thread is still alive (gate not released) — restart must refuse.
    spawned = mon.restart_account_thread("Rosa")

    assert spawned is False
    # No new invocation of _monitor_loop.
    assert stub_loop.calls == ["Rosa"]
    # Same thread object.
    assert mon._threads["Rosa"] is t1

    # Cleanup.
    stub_loop.gate_for("Rosa").set()
    t1.join(timeout=2.0)


def test_restart_spawns_when_previous_thread_dead(stub_loop):
    mon = _new_monitor(["Rosa"])
    mon.start()
    t1 = _wait_for_thread_alive(mon, "Rosa")

    # Let the original thread exit.
    stub_loop.gate_for("Rosa").set()
    t1.join(timeout=2.0)
    assert not t1.is_alive()

    # Reset the gate so the *next* spawn also blocks (lets us observe alive).
    stub_loop.gate_for("Rosa").clear()

    spawned = mon.restart_account_thread("Rosa")

    assert spawned is True
    assert stub_loop.calls == ["Rosa", "Rosa"]
    t2 = _wait_for_thread_alive(mon, "Rosa")
    assert t2 is not t1

    # Cleanup.
    stub_loop.gate_for("Rosa").set()
    t2.join(timeout=2.0)


def test_restart_returns_true_immediately_does_not_block(stub_loop):
    """β retry must not block on connect — caller does its own sleep+poll."""
    mon = _new_monitor(["Rosa"])

    start = time.time()
    spawned = mon.restart_account_thread("Rosa")
    elapsed = time.time() - start

    assert spawned is True
    assert elapsed < 0.5, f"restart_account_thread blocked for {elapsed:.2f}s"

    # Cleanup.
    stub_loop.gate_for("Rosa").set()
    t = mon._threads["Rosa"]
    if t:
        t.join(timeout=2.0)
