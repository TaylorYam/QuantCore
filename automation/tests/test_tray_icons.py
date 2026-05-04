"""
Unit tests for tray_icons — compositing the QuantCore base icon with a
colored status dot in the lower-right quadrant.

Tests check both API surface (size / mode / file existence) and visual
invariants (dot pixel is the requested colour, top-left preserves base).

No tray runtime needed — pure PIL operations.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PIL import Image


# Path to the project's existing brand icon (RGBA 256x256).
BASE_ICO = Path(__file__).resolve().parent.parent / "quantcore.ico"


# ── compose_tray_icon ──────────────────────────────────────────────

def test_compose_returns_rgba_image_of_requested_size():
    from tray_icons import compose_tray_icon
    img = compose_tray_icon(BASE_ICO, "green", 32)
    assert img.mode == "RGBA"
    assert img.size == (32, 32)


@pytest.mark.parametrize("size", [16, 24, 32, 48])
def test_compose_supports_all_required_sizes(size):
    from tray_icons import compose_tray_icon
    img = compose_tray_icon(BASE_ICO, "green", size)
    assert img.size == (size, size)


def test_compose_pastes_color_dot_in_lower_right_quadrant():
    """Sample a pixel in the dot's expected centre — should match the
    requested colour (alpha = 255) within a tolerance for anti-aliasing."""
    from tray_icons import compose_tray_icon, COLORS

    size = 48
    img = compose_tray_icon(BASE_ICO, "green", size)
    # Dot is centred in the lower-right quadrant.
    cx, cy = int(size * 0.75), int(size * 0.75)
    r, g, b, a = img.getpixel((cx, cy))

    expected_hex = COLORS["green"].lstrip("#")
    er, eg, eb = (int(expected_hex[i:i+2], 16) for i in (0, 2, 4))

    assert a == 255, f"dot centre should be opaque (got alpha={a})"
    # Tolerance ±3 per channel for downsizing artifacts.
    assert abs(r - er) <= 3
    assert abs(g - eg) <= 3
    assert abs(b - eb) <= 3


def test_compose_preserves_top_left_quadrant_of_base():
    """The dot lives in the lower-right; the upper-left quadrant must remain
    pixel-identical to the (downsized) base icon."""
    from tray_icons import compose_tray_icon

    size = 48
    composited = compose_tray_icon(BASE_ICO, "red", size)

    base = Image.open(BASE_ICO).convert("RGBA").resize((size, size), Image.LANCZOS)

    # Sample several points in the upper-left quadrant (avoid centre crossings).
    for x in (4, 10, 16):
        for y in (4, 10, 16):
            assert composited.getpixel((x, y)) == base.getpixel((x, y)), \
                f"pixel ({x},{y}) should be unchanged from base"


def test_compose_different_colors_produce_different_dot_pixels():
    """Sanity: changing the colour parameter actually changes the output."""
    from tray_icons import compose_tray_icon

    size = 32
    cx, cy = int(size * 0.75), int(size * 0.75)
    green = compose_tray_icon(BASE_ICO, "green", size).getpixel((cx, cy))
    red = compose_tray_icon(BASE_ICO, "red", size).getpixel((cx, cy))
    gray = compose_tray_icon(BASE_ICO, "gray", size).getpixel((cx, cy))

    assert green != red
    assert green != gray
    assert red != gray


def test_compose_rejects_unknown_color():
    from tray_icons import compose_tray_icon
    with pytest.raises((KeyError, ValueError)):
        compose_tray_icon(BASE_ICO, "purple", 32)


# ── ensure_tray_icons ──────────────────────────────────────────────

def test_ensure_creates_all_color_size_combinations(tmp_path):
    from tray_icons import ensure_tray_icons, COLORS, SIZES

    ensure_tray_icons(tmp_path, BASE_ICO)

    for color in COLORS:
        for size in SIZES:
            f = tmp_path / f"quantcore_{color}_{size}.png"
            assert f.exists(), f"missing: {f.name}"
            # Sanity: file is a real PNG of the right size.
            with Image.open(f) as img:
                assert img.size == (size, size)


def test_ensure_skips_existing_files(tmp_path):
    """If a target file already exists, ensure_tray_icons must not overwrite
    it — established by leaving placeholder content untouched."""
    from tray_icons import ensure_tray_icons

    pre_existing = tmp_path / "quantcore_green_32.png"
    placeholder_bytes = b"NOT A PNG SENTINEL"
    pre_existing.write_bytes(placeholder_bytes)

    ensure_tray_icons(tmp_path, BASE_ICO)

    # Placeholder must be untouched; other files must have been generated.
    assert pre_existing.read_bytes() == placeholder_bytes
    assert (tmp_path / "quantcore_red_32.png").exists()


def test_ensure_idempotent_second_call_no_op(tmp_path):
    """Running twice yields the same files; mtime of round 1 files preserved."""
    from tray_icons import ensure_tray_icons

    ensure_tray_icons(tmp_path, BASE_ICO)
    sample = tmp_path / "quantcore_gray_24.png"
    mtime_before = sample.stat().st_mtime

    # Sleep a beat so mtime would visibly differ if rewritten.
    time.sleep(0.05)
    ensure_tray_icons(tmp_path, BASE_ICO)

    assert sample.stat().st_mtime == mtime_before


def test_ensure_creates_out_dir_if_missing(tmp_path):
    """First run on a fresh install: assets/tray/ doesn't exist yet."""
    from tray_icons import ensure_tray_icons, COLORS, SIZES

    out_dir = tmp_path / "fresh" / "nested"
    assert not out_dir.exists()

    ensure_tray_icons(out_dir, BASE_ICO)

    # All files generated into the new dir.
    expected_count = len(COLORS) * len(SIZES)
    assert len(list(out_dir.glob("*.png"))) == expected_count
