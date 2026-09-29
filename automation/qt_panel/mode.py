"""新 Panel 的模式：並行模式（只看不下單）或正式模式（ADR-0012）。

模式只由 config.json 的 `qt_panel_mode` 一個設定決定，預設並行模式。
正式模式（值班、實盤送單交給新 Panel）在切換工單 #154 才提供；在那之前
任何其他值都退回並行模式，並附上給畫面顯示的說明。
"""
from __future__ import annotations

from enum import Enum
from typing import Mapping

SETTING_KEY = "qt_panel_mode"


class PanelMode(Enum):
    PARALLEL = "parallel"


def resolve_mode(config: Mapping) -> tuple[PanelMode, str]:
    """回傳 (模式, 說明)。說明為空字串代表設定照原樣生效。"""
    raw = config.get(SETTING_KEY, PanelMode.PARALLEL.value)
    if raw == PanelMode.PARALLEL.value:
        return PanelMode.PARALLEL, ""
    if raw == "production":
        return PanelMode.PARALLEL, "正式模式尚未提供（切換工單 #154），先以並行模式執行"
    return PanelMode.PARALLEL, f"不認得的 {SETTING_KEY} 設定 {raw!r}，以並行模式執行"
