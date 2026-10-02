"""新 Panel 的模式：並行模式（只看不下單）或正式模式（ADR-0012）。

模式只由 config.json 的 `qt_panel_mode` 一個設定決定，預設並行模式。
正式模式（#154）＝值班與實盤送單交給新 Panel；Tk Panel 讀同一個設定
（`qt_panel_owns_live`），正式模式下自己不值班、不送實盤單。切換與回退都只改
這一個字，不會出現兩邊同時值班或都沒人值班。不認得的值一律退回並行模式。

不 import PySide6（Tk Panel 也用）。
"""
from __future__ import annotations

from enum import Enum
from typing import Mapping

SETTING_KEY = "qt_panel_mode"


class PanelMode(Enum):
    PARALLEL = "parallel"
    PRODUCTION = "production"


def resolve_mode(config: Mapping) -> tuple[PanelMode, str]:
    """回傳 (模式, 說明)。說明為空字串代表設定照原樣生效。"""
    raw = config.get(SETTING_KEY, PanelMode.PARALLEL.value)
    for mode in PanelMode:
        if raw == mode.value:
            return mode, ""
    return PanelMode.PARALLEL, f"不認得的 {SETTING_KEY} 設定 {raw!r}，以並行模式執行"


def qt_panel_owns_live(config: Mapping) -> bool:
    """值班與實盤送單是否已交給新 Panel（Tk Panel 據此停掉自己的值班與送單）。"""
    return resolve_mode(config)[0] is PanelMode.PRODUCTION
