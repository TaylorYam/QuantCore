"""
Compose QuantCore tray icons from the brand `.ico` plus a colored status dot
in the lower-right quadrant.

Visual rationale: the reference taiwan-momentum-swing tray uses pure colored
circles, so QuantCore's "logo + dot" makes the two trays distinguishable at
a glance on the same machine (PRD US-5).

Three colors × four sizes = 12 PNGs cached under `automation/assets/tray/`,
generated on first launch.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# Status colours (hex). Match taiwan-momentum-swing's palette so the colour
# language is consistent across both apps even though the icon shape differs.
COLORS: dict[str, str] = {
    "gray":  "#808080",
    "green": "#28a745",
    "red":   "#dc3545",
}

# Windows tray supports a few stock sizes; pre-rendering avoids
# runtime resampling artifacts.
SIZES: tuple[int, ...] = (16, 24, 32, 48)

# Dot geometry — fraction of the final icon size.
# The dot occupies a square in the lower-right quadrant. Centre at (75%, 75%),
# diameter = 50% of icon size (leaves a small inset from each edge).
_DOT_CENTER_FRAC = 0.75
_DOT_DIAMETER_FRAC = 0.50

# Render at 4x then down-sample so the dot edge stays smooth at 16px.
_SUPERSAMPLE = 4


def _hex_to_rgba(hex_color: str) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


def compose_tray_icon(base_path: Path, color: str, size: int) -> Image.Image:
    """Read the QuantCore base ICO, paste a colored dot in the lower-right
    quadrant, return a resized RGBA Image of the requested size."""
    if color not in COLORS:
        raise KeyError(f"unknown color {color!r}; valid: {sorted(COLORS)}")

    # Render at supersample resolution for clean down-scaling.
    big = size * _SUPERSAMPLE
    base = Image.open(base_path).convert("RGBA").resize((big, big), Image.LANCZOS)

    overlay = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cx = int(big * _DOT_CENTER_FRAC)
    cy = int(big * _DOT_CENTER_FRAC)
    radius = int((big * _DOT_DIAMETER_FRAC) / 2)
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=_hex_to_rgba(COLORS[color]),
    )

    composited = Image.alpha_composite(base, overlay)
    return composited.resize((size, size), Image.LANCZOS)


def _icon_filename(color: str, size: int) -> str:
    return f"quantcore_{color}_{size}.png"


def ensure_tray_icons(
    out_dir: Path,
    base_path: Path,
    colors: tuple[str, ...] = tuple(COLORS),
    sizes: tuple[int, ...] = SIZES,
) -> None:
    """Generate all color × size variants under `out_dir`, skipping any file
    that already exists (idempotent — safe to call on every launch).

    `out_dir` is created if missing."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for color in colors:
        for size in sizes:
            target = out_dir / _icon_filename(color, size)
            if target.exists():
                continue
            img = compose_tray_icon(base_path, color, size)
            img.save(target, format="PNG")
