"""
ibc_cmd — talk to IBC's CommandServer over a local TCP socket.

Used to work around IBC 3.23.0's broken UI scraper (can't find the
"Auto Logoff / Auto Restart Time" controls in Gateway 10.41). Instead
of relying on the scheduled restart that IBC tries to program via
screen-scraping, we directly send a `RESTART` command to each IBC-managed
account's CommandServer.

IBC CommandServer protocol (v3.x)
---------------------------------
- Plain TCP socket on 127.0.0.1:<CommandServerPort> (configured per
  account in `C:\\Jts\\gateway_<x>\\ibc\\config.ini` as `CommandServerPort`).
- Command is a single ASCII line terminated by `\\n` or `\\r\\n`.
- Response is a single ASCII line: starts with `OK` on success, or
  `ERROR ...` on failure.
- Some commands (RESTART, STOP) close the connection server-side after
  acknowledging, so we must read with a short timeout and not assume
  the connection stays open.

Supported commands
------------------
- ENABLEAPI      — toggle the Read-Only API checkbox on (benign test ping)
- RESTART        — Gateway warm restart (no 2FA, uses cached credentials)
- STOP           — full Gateway shutdown (DESTRUCTIVE, avoid in production)

Usage
-----
    from ibc_cmd import send_command
    resp = send_command("127.0.0.1", 7462, "RESTART")
    if not resp.upper().startswith("OK"):
        raise RuntimeError(f"IBC RESTART failed: {resp!r}")
"""

from __future__ import annotations

import logging
import socket
import time

log = logging.getLogger(__name__)


class IBCCommandError(RuntimeError):
    """IBC CommandServer returned an error or the connection failed."""


def send_command(
    host: str,
    port: int,
    command: str,
    timeout: float = 10.0,
) -> str:
    """
    Send a one-shot command to IBC CommandServer and return its response.

    Parameters
    ----------
    host : str
        Usually "127.0.0.1". IBC binds loopback by default.
    port : int
        The `CommandServerPort` value from the account's IBC config.ini
        (7462 / 7463 / 7464 for Taylor / Rosa / Yvonne in QuantCore).
    command : str
        One of the supported IBC commands. Case-insensitive. Do not
        include a trailing newline — we add `\\r\\n`.
    timeout : float
        Total wall-clock budget for connect + send + recv, in seconds.

    Returns
    -------
    str
        Response line with leading/trailing whitespace stripped. Typical
        values: `"OK"`, `"ERROR Command invalid"`, or a longer server
        message. Empty string means the server closed the socket
        without a reply (some versions do this for RESTART/STOP).

    Raises
    ------
    IBCCommandError
        Connection refused, timed out, or any other socket-level failure.
    """
    deadline = time.monotonic() + timeout
    cmd_bytes = (command.strip() + "\r\n").encode("ascii")

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(cmd_bytes)

            # Read until newline, EOF, or deadline.
            buf = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(min(remaining, 1.0))
                try:
                    chunk = sock.recv(1024)
                except socket.timeout:
                    # Some commands (RESTART) may not reply; bail gracefully.
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\n" in chunk:
                    break
    except ConnectionRefusedError as e:
        raise IBCCommandError(
            f"IBC CommandServer at {host}:{port} refused the connection "
            f"(is IBC running for this account?)"
        ) from e
    except OSError as e:
        raise IBCCommandError(
            f"IBC CommandServer at {host}:{port} failed: {e}"
        ) from e

    return buf.decode("ascii", errors="replace").strip()


def restart_gateway(host: str, port: int, timeout: float = 10.0) -> str:
    """
    Convenience wrapper: send RESTART to the named IBC CommandServer.

    Returns the response string for logging. Raises IBCCommandError on
    connect failure. Does NOT raise on "ERROR ..." responses — caller
    should inspect the return value and decide whether to escalate.
    """
    resp = send_command(host, port, "RESTART", timeout=timeout)
    log.info(f"IBC RESTART {host}:{port} → {resp!r}")
    return resp
