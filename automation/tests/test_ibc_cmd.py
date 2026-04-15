"""
Unit tests for ibc_cmd — the IBC CommandServer socket helper.

Starts a tiny in-process TCP server that mimics IBC's protocol
(read one line, write one line, close), so these tests never touch
a real IB Gateway and can run in any CI environment.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from ibc_cmd import IBCCommandError, send_command, restart_gateway


class _FakeIBC:
    """Tiny TCP server that behaves like IBC CommandServer for one request.

    Each connection is handled in a fresh thread:
      - read one line (terminator \\n)
      - look it up in responses dict
      - send the response + \\r\\n
      - close

    Usage:
        with _FakeIBC({"RESTART": "OK", "BAD": "ERROR Command invalid"}) as srv:
            resp = send_command("127.0.0.1", srv.port, "RESTART")
    """

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        *,
        send_reply: bool = True,
        close_before_reply: bool = False,
    ):
        self.responses = responses or {}
        self.send_reply = send_reply
        self.close_before_reply = close_before_reply
        self.received: list[str] = []

        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]

        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # Context manager support
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        self._stop = True
        try:
            self._srv.close()
        except Exception:
            pass
        self._thread.join(timeout=1.0)

    def _run(self):
        try:
            while not self._stop:
                self._srv.settimeout(0.2)
                try:
                    client, _ = self._srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                threading.Thread(target=self._handle, args=(client,),
                                 daemon=True).start()
        except Exception:
            pass

    def _handle(self, client: socket.socket):
        try:
            client.settimeout(1.0)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = client.recv(1024)
                if not chunk:
                    break
                buf.extend(chunk)
            line = buf.decode("ascii", errors="replace").strip()
            self.received.append(line)

            if self.close_before_reply:
                return

            if self.send_reply:
                resp = self.responses.get(line.upper(), "ERROR Unknown")
                client.sendall((resp + "\r\n").encode("ascii"))
        except Exception:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass


# ── Tests ───────────────────────────────────────────────────────────


def test_send_command_ok_response():
    with _FakeIBC({"RESTART": "OK"}) as srv:
        resp = send_command("127.0.0.1", srv.port, "RESTART", timeout=3.0)
    assert resp == "OK"


def test_send_command_error_response():
    with _FakeIBC({}) as srv:
        resp = send_command("127.0.0.1", srv.port, "BOGUS", timeout=3.0)
    assert resp.startswith("ERROR")


def test_send_command_case_preserved_on_wire():
    """Verify we send exactly what the caller asked for (stripped + \\r\\n)."""
    with _FakeIBC({"RESTART": "OK"}) as srv:
        send_command("127.0.0.1", srv.port, "  RESTART  ", timeout=3.0)
    assert srv.received == ["RESTART"]


def test_send_command_refuses_when_no_server():
    # Bind a socket then close it — port is unlikely to be re-used for 50ms
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    with pytest.raises(IBCCommandError):
        send_command("127.0.0.1", port, "RESTART", timeout=1.0)


def test_send_command_empty_reply_on_socket_close():
    """Some IBC versions close the socket after RESTART without replying.
    We should treat that as an empty string, not an error."""
    with _FakeIBC({}, send_reply=False, close_before_reply=True) as srv:
        resp = send_command("127.0.0.1", srv.port, "RESTART", timeout=2.0)
    assert resp == ""


def test_restart_gateway_wraps_send_command():
    with _FakeIBC({"RESTART": "OK"}) as srv:
        resp = restart_gateway("127.0.0.1", srv.port, timeout=3.0)
    assert resp == "OK"
