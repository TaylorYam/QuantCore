"""
TrayController — pystray wrapper for QuantCore Control Panel.

Hides pystray's native event loop behind a small surface so:
  * `control_panel` doesn't import pystray directly,
  * tray failures (e.g. Windows tray service down) soft-fail without
    propagating to the panel startup,
  * tests can inject a fake icon factory and run in microseconds.

Slice 1 ships only the lifecycle (`start` / `stop`) plus the menu wiring;
`update_state` is present but a no-op until Slice 2 adds colour swapping.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image

from tray_icons import _icon_filename

log = logging.getLogger(__name__)

# Icon size loaded into pystray. Windows tray scales as needed; 32 is the
# safe default. The PNG cache holds 16/24/32/48 already so a future Slice
# could pick per-DPI sizes if needed.
_TRAY_ICON_SIZE = 32

_INITIAL_COLOR = "gray"


def _default_icon_factory(*a, **k):
    """Real pystray.Icon constructor (lazy-imported so a missing pystray
    install becomes a soft-fail at start() time, not import time)."""
    import pystray
    return pystray.Icon(*a, **k)


class TrayController:
    """Wraps a pystray.Icon: builds the menu, starts/stops the native
    event loop, and exposes update_state for status colour swapping."""

    def __init__(
        self,
        assets_dir: Path,
        on_show_window: Callable[[], None],
        on_quit: Callable[[], None],
        icon_factory: Callable[..., Any] = _default_icon_factory,
    ) -> None:
        self._assets = Path(assets_dir)
        self._on_show = on_show_window
        self._on_quit = on_quit
        self._icon_factory = icon_factory
        self._icon: Optional[Any] = None

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> bool:
        """Build pystray Icon, attach menu, run in detached thread.

        Returns False (and logs a warning) if pystray is missing or any
        part of init raises — the caller falls back to the legacy
        close-destroys-app behavior per PRD FR-T9."""
        try:
            import pystray  # local: surface the soft-fail here, not at import.

            image = self._load_icon(_INITIAL_COLOR)
            menu = pystray.Menu(
                pystray.MenuItem(
                    "打開 Panel",
                    self._on_open_clicked,
                    default=True,  # left-click on tray fires this
                ),
                pystray.MenuItem("離開", self._on_quit_clicked),
            )
            self._icon = self._icon_factory(
                "QuantCore",
                image,
                "QuantCore — Rosa: INIT",
                menu,
            )
            self._icon.run_detached()
            return True
        except Exception as e:
            log.warning(f"TrayController.start: tray init failed ({e!r}) — "
                        f"falling back to legacy close behavior")
            self._icon = None
            return False

    def stop(self) -> None:
        """Stop pystray's native loop. Safe to call before start()."""
        icon = self._icon
        if icon is None:
            return
        try:
            icon.stop()
        except Exception as e:
            log.warning(f"TrayController.stop: {e!r}")

    def is_alive(self) -> bool:
        """Return True if the tray icon's background thread is still running."""
        if self._icon is None:
            return False
        try:
            thread = getattr(self._icon, '_detach_thread', None)
            if thread is not None:
                return thread.is_alive()
            return getattr(self._icon, 'visible', False)
        except Exception:
            return False

    # ── status update ──────────────────────────────────────────────

    # GatewayMonitor reports four states; the tray surfaces three colours.
    # init / connecting are both pre-online → gray (no signal yet).
    _STATE_TO_COLOR = {
        "init":       "gray",
        "connecting": "gray",
        "online":     "green",
        "offline":    "red",
    }

    def update_state(self, state: str, last_ts: str = "") -> None:
        """Repaint the tray icon and tooltip to reflect the latest ROSA
        connection state.

        * Unknown state defaults to gray (defensive against future
          GatewayMonitor states the tray hasn't been taught about).
        * No-op if the icon hasn't been started yet (Slice 1 timing
          race during panel boot).
        """
        if self._icon is None:
            return

        color = self._STATE_TO_COLOR.get(state, "gray")
        try:
            self._icon.icon = self._load_icon(color)
            suffix = f" (last {last_ts})" if last_ts else ""
            self._icon.title = f"QuantCore — Rosa: {state.upper()}{suffix}"
        except Exception as e:
            log.warning(f"TrayController.update_state({state!r}): {e!r}")

    # ── internal: menu callbacks (pystray passes (icon, item)) ─────

    def _on_open_clicked(self, icon, item):
        self._on_show()

    def _on_quit_clicked(self, icon, item):
        self._on_quit()

    # ── internal: icon loading ─────────────────────────────────────

    def _load_icon(self, color: str) -> Image.Image:
        path = self._assets / _icon_filename(color, _TRAY_ICON_SIZE)
        return Image.open(path).convert("RGBA")
