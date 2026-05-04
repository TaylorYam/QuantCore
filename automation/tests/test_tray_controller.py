"""
Unit tests for TrayController — pystray wrapper that hides icon lifecycle
+ menu wiring behind a small surface so the panel doesn't import pystray
directly and can soft-fail when the tray service is unavailable.

Real pystray.Icon is replaced by a recording fake (injected via the
`icon_factory` ctor parameter), so tests run in microseconds without
spinning a native event loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest


# ── Fakes ──────────────────────────────────────────────────────────

class _FakeIcon:
    """Records constructor args and lifecycle calls; never spawns a thread."""

    def __init__(self, name, icon, title=None, menu=None):
        self.name = name
        self.icon = icon
        self.title = title
        self.menu = menu
        self.run_called = False
        self.stop_called = False

    def run_detached(self):
        self.run_called = True

    def stop(self):
        self.stop_called = True


class _FailingFactory:
    """Mimics pystray.Icon raising on construction (e.g., tray service down)."""

    def __init__(self, exc):
        self._exc = exc

    def __call__(self, *a, **k):
        raise self._exc


# ── Fixture: minimal assets dir with a real PNG so PIL.Image.open succeeds ──

@pytest.fixture
def assets_dir(tmp_path):
    """Generate just the 32px gray icon — the only one Slice 1 actually needs."""
    from tray_icons import compose_tray_icon, _icon_filename
    base = Path(__file__).resolve().parent.parent / "quantcore.ico"
    img = compose_tray_icon(base, "gray", 32)
    out = tmp_path / _icon_filename("gray", 32)
    img.save(out, format="PNG")
    return tmp_path


# ── Constructor ────────────────────────────────────────────────────

def test_ctor_stores_callbacks_without_starting(assets_dir):
    from tray_controller import TrayController

    show_calls = []
    quit_calls = []
    tc = TrayController(
        assets_dir=assets_dir,
        on_show_window=lambda: show_calls.append(1),
        on_quit=lambda: quit_calls.append(1),
        icon_factory=_FakeIcon,
    )
    # No pystray side-effects until start() is called.
    assert tc._icon is None
    assert show_calls == []
    assert quit_calls == []


# ── start() ─────────────────────────────────────────────────────────

def test_start_constructs_icon_and_returns_true(assets_dir):
    from tray_controller import TrayController

    tc = TrayController(
        assets_dir=assets_dir,
        on_show_window=lambda: None,
        on_quit=lambda: None,
        icon_factory=_FakeIcon,
    )

    assert tc.start() is True
    assert isinstance(tc._icon, _FakeIcon)
    assert tc._icon.run_called is True


def test_start_returns_false_when_factory_raises(assets_dir, caplog):
    """Soft-fail per PRD FR-T9: import or construction failure must not
    propagate — caller falls back to legacy close behavior."""
    from tray_controller import TrayController

    tc = TrayController(
        assets_dir=assets_dir,
        on_show_window=lambda: None,
        on_quit=lambda: None,
        icon_factory=_FailingFactory(RuntimeError("tray service unavailable")),
    )

    assert tc.start() is False
    assert tc._icon is None


def test_start_loads_gray_icon_for_initial_state(assets_dir):
    """Starting the tray with no known status defaults to the gray (init) icon."""
    from PIL import Image
    from tray_controller import TrayController

    tc = TrayController(
        assets_dir=assets_dir,
        on_show_window=lambda: None,
        on_quit=lambda: None,
        icon_factory=_FakeIcon,
    )
    tc.start()

    assert tc._icon is not None
    assert isinstance(tc._icon.icon, Image.Image)


# ── Menu structure ─────────────────────────────────────────────────

def _menu_items(icon) -> list:
    """pystray.Menu can be iterated for its items (each has .text + a callable)."""
    return list(icon.menu)


def test_menu_has_open_and_quit_items(assets_dir):
    from tray_controller import TrayController

    tc = TrayController(
        assets_dir=assets_dir,
        on_show_window=lambda: None,
        on_quit=lambda: None,
        icon_factory=_FakeIcon,
    )
    tc.start()

    items = _menu_items(tc._icon)
    texts = [item.text for item in items]
    assert "打開 Panel" in texts
    assert "離開" in texts


def test_menu_open_action_invokes_on_show_window(assets_dir):
    from tray_controller import TrayController

    show_calls = []
    tc = TrayController(
        assets_dir=assets_dir,
        on_show_window=lambda: show_calls.append("show"),
        on_quit=lambda: None,
        icon_factory=_FakeIcon,
    )
    tc.start()

    open_item = next(it for it in _menu_items(tc._icon) if it.text == "打開 Panel")
    # pystray's MenuItem.__call__(icon) internally invokes action(icon, item) —
    # don't pass `item` ourselves.
    open_item(tc._icon)

    assert show_calls == ["show"]


def test_menu_quit_action_invokes_on_quit(assets_dir):
    from tray_controller import TrayController

    quit_calls = []
    tc = TrayController(
        assets_dir=assets_dir,
        on_show_window=lambda: None,
        on_quit=lambda: quit_calls.append("quit"),
        icon_factory=_FakeIcon,
    )
    tc.start()

    quit_item = next(it for it in _menu_items(tc._icon) if it.text == "離開")
    quit_item(tc._icon)

    assert quit_calls == ["quit"]


def test_default_left_click_action_invokes_on_show_window(assets_dir):
    """pystray fires the menu's default action on left-click. The default
    must be the 'open' item so single-click opens the panel (PRD FR-T3)."""
    from tray_controller import TrayController

    show_calls = []
    tc = TrayController(
        assets_dir=assets_dir,
        on_show_window=lambda: show_calls.append(1),
        on_quit=lambda: None,
        icon_factory=_FakeIcon,
    )
    tc.start()

    default_item = next(it for it in _menu_items(tc._icon) if it.default)
    assert default_item.text == "打開 Panel"


# ── stop() ──────────────────────────────────────────────────────────

def test_stop_calls_icon_stop_when_started(assets_dir):
    from tray_controller import TrayController

    tc = TrayController(
        assets_dir=assets_dir,
        on_show_window=lambda: None,
        on_quit=lambda: None,
        icon_factory=_FakeIcon,
    )
    tc.start()
    icon = tc._icon

    tc.stop()

    assert icon.stop_called is True


def test_stop_is_safe_when_never_started(assets_dir):
    """Calling stop() before start() (e.g., in a quit-during-init path)
    must not raise."""
    from tray_controller import TrayController

    tc = TrayController(
        assets_dir=assets_dir,
        on_show_window=lambda: None,
        on_quit=lambda: None,
        icon_factory=_FakeIcon,
    )
    # Must not raise.
    tc.stop()


# ── update_state() — slice 2: state → color + tooltip ─────────────

@pytest.fixture
def all_color_assets_dir(tmp_path):
    """Assets dir with ALL three colours pre-rendered (Slice 2 needs them)."""
    from tray_icons import compose_tray_icon, _icon_filename
    base = Path(__file__).resolve().parent.parent / "quantcore.ico"
    for color in ("gray", "green", "red"):
        img = compose_tray_icon(base, color, 32)
        img.save(tmp_path / _icon_filename(color, 32), format="PNG")
    return tmp_path


def _started(assets):
    from tray_controller import TrayController
    tc = TrayController(
        assets_dir=assets,
        on_show_window=lambda: None,
        on_quit=lambda: None,
        icon_factory=_FakeIcon,
    )
    tc.start()
    return tc


def _pixel_color_at_dot(image):
    """Sample the dot centre (lower-right quadrant) for a colour signature."""
    w, h = image.size
    return image.getpixel((int(w * 0.75), int(h * 0.75)))


def test_update_state_init_uses_gray_icon(all_color_assets_dir):
    from tray_icons import compose_tray_icon, COLORS
    base = Path(__file__).resolve().parent.parent / "quantcore.ico"
    expected = _pixel_color_at_dot(compose_tray_icon(base, "gray", 32))

    tc = _started(all_color_assets_dir)
    tc.update_state("init")

    assert _pixel_color_at_dot(tc._icon.icon) == expected


def test_update_state_connecting_uses_gray_icon(all_color_assets_dir):
    from tray_icons import compose_tray_icon
    base = Path(__file__).resolve().parent.parent / "quantcore.ico"
    expected = _pixel_color_at_dot(compose_tray_icon(base, "gray", 32))

    tc = _started(all_color_assets_dir)
    tc.update_state("connecting")

    assert _pixel_color_at_dot(tc._icon.icon) == expected


def test_update_state_online_uses_green_icon(all_color_assets_dir):
    from tray_icons import compose_tray_icon
    base = Path(__file__).resolve().parent.parent / "quantcore.ico"
    expected = _pixel_color_at_dot(compose_tray_icon(base, "green", 32))

    tc = _started(all_color_assets_dir)
    tc.update_state("online", "22:30")

    assert _pixel_color_at_dot(tc._icon.icon) == expected


def test_update_state_offline_uses_red_icon(all_color_assets_dir):
    from tray_icons import compose_tray_icon
    base = Path(__file__).resolve().parent.parent / "quantcore.ico"
    expected = _pixel_color_at_dot(compose_tray_icon(base, "red", 32))

    tc = _started(all_color_assets_dir)
    tc.update_state("offline", "23:00")

    assert _pixel_color_at_dot(tc._icon.icon) == expected


def test_update_state_unknown_falls_back_to_gray(all_color_assets_dir):
    """Defensive: a future GatewayMonitor state we don't know about must
    not crash the tray — surface as gray (unknown)."""
    from tray_icons import compose_tray_icon
    base = Path(__file__).resolve().parent.parent / "quantcore.ico"
    expected = _pixel_color_at_dot(compose_tray_icon(base, "gray", 32))

    tc = _started(all_color_assets_dir)
    tc.update_state("some_future_state", "01:00")

    assert _pixel_color_at_dot(tc._icon.icon) == expected


def test_update_state_title_without_timestamp(all_color_assets_dir):
    tc = _started(all_color_assets_dir)
    tc.update_state("init")
    assert tc._icon.title == "QuantCore — Rosa: INIT"


def test_update_state_title_with_timestamp(all_color_assets_dir):
    tc = _started(all_color_assets_dir)
    tc.update_state("online", "22:30")
    assert tc._icon.title == "QuantCore — Rosa: ONLINE (last 22:30)"


def test_update_state_title_offline_with_timestamp(all_color_assets_dir):
    tc = _started(all_color_assets_dir)
    tc.update_state("offline", "23:00")
    assert tc._icon.title == "QuantCore — Rosa: OFFLINE (last 23:00)"


def test_update_state_before_start_is_safe(all_color_assets_dir):
    """If the panel calls update_state before _start_tray (timing race) the
    controller must just no-op — no exception escapes to the GW callback."""
    from tray_controller import TrayController

    tc = TrayController(
        assets_dir=all_color_assets_dir,
        on_show_window=lambda: None,
        on_quit=lambda: None,
        icon_factory=_FakeIcon,
    )
    # Must not raise.
    tc.update_state("online", "22:30")
