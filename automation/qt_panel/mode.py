"""新 Panel 的模式：並行模式（只看不下單）或正式模式（ADR-0012）。

模式只由 config.json 的 `qt_panel_mode` 一個設定決定，預設並行模式。
正式模式（#154）＝值班與實盤送單交給新 Panel；Tk Panel 讀同一個設定
（`qt_panel_owns_live`），正式模式下自己不值班、不送實盤單。切換與回退都只改
這一個字。不認得的值一律退回並行模式。

兩個 Panel 都只在開啟時讀設定，改了設定卻沒按順序重開，可能兩邊都以為自己值班。
所以要值班的那一邊開啟時先拿「值班鎖」（`claim_live_duty`）：另一個 Panel 正拿著
就不值班、不送實盤單，畫面說明要先關哪一個。先值班的那一邊繼續值班，所以不會同時
值班，也不會兩邊都沒人值班。

不 import PySide6（Tk Panel 也用）。
"""
from __future__ import annotations

import tempfile
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

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


# ── 值班鎖：值班與實盤送單同時只歸一個 Panel ─────────────────────────

LIVE_DUTY_LOCK = Path(tempfile.gettempdir()) / "quantcore_live_duty.lock"

QT_OWNS_LIVE_TEXT = (
    "正式模式（config.json qt_panel_mode = production）：值班與實盤送單已交給新 Panel，"
    "Tk Panel 只看、不值班、不能送單。要改回 Tk，把 qt_panel_mode 改回 parallel，"
    "關掉新 Panel 後重開 Tk Panel（見 docs/qt-panel-cutover.md 的回退步驟）。")
QT_STILL_ON_DUTY_TEXT = (
    "新 Panel 正在值班與送實盤單（它開啟時是正式模式），所以 Tk Panel 這次只看、"
    "不值班、不能送單。要改回 Tk 值班：關掉新 Panel 與 Tk Panel，確認 qt_panel_mode "
    "是 parallel，再重開 Tk Panel。")
TK_STILL_ON_DUTY_TEXT = (
    "config.json 是正式模式，但 Tk Panel 還在值班（它開啟時是並行模式），所以新 Panel "
    "這次以並行模式執行：不值班、不送實盤單。請關掉 Tk Panel 與新 Panel，再重開新 Panel。")


def claim_live_duty(path: Path | None = None) -> bool:
    """拿值班鎖；另一個還活著的 Panel 拿著就回傳 False。"""
    from single_instance import acquire_lock
    return acquire_lock(path or LIVE_DUTY_LOCK)


def release_live_duty(path: Path | None = None, *, held: bool) -> None:
    """Panel 離開時放掉自己拿到的值班鎖；沒拿到的不能刪（那是別人的）。"""
    if held:
        from single_instance import release_lock
        release_lock(path or LIVE_DUTY_LOCK)


def qt_panel_duty(config: Mapping, claim: Callable[[], bool] = claim_live_duty
                  ) -> tuple[PanelMode, str]:
    """新 Panel 實際用的模式：正式模式要拿得到值班鎖，否則退回並行模式並說明。"""
    mode, note = resolve_mode(config)
    if mode is PanelMode.PRODUCTION and not claim():
        return PanelMode.PARALLEL, TK_STILL_ON_DUTY_TEXT
    return mode, note


def tk_panel_duty(config: Mapping, claim: Callable[[], bool] = claim_live_duty
                  ) -> tuple[bool, str]:
    """Tk Panel 是否只看：(只看, 原因)。正式模式只看；並行模式要拿得到值班鎖。"""
    if qt_panel_owns_live(config):
        return True, QT_OWNS_LIVE_TEXT
    if not claim():
        return True, QT_STILL_ON_DUTY_TEXT
    return False, ""
