"""
QuantCore — 英數欄位不經過輸入法（#138）

Windows 中文輸入法（注音）開著中文模式時，`.` 是注音符號 ㄡ：打 `184.4`
會把 `.4` 吃進組字，欄位只剩 `1844`，而且是合法數字，不會報錯。

`ascii_entry` 讓 Tk Entry 取得焦點時停用輸入法、離開時恢復；萬一仍收到
全形字元（全形數字、`．`、`。`）就轉成半形，讓後續解析照常報錯或通過，
不會默默丟掉字元。非 Windows 或 imm32 不可用時只做全形轉換。
"""

from __future__ import annotations

import logging
import sys
import unicodedata

log = logging.getLogger(__name__)

# NFKC 處理全形英數與「．」；「。」（含半形「｡」經 NFKC 後）另外轉成小數點。
_IDEOGRAPHIC_STOPS = str.maketrans({"。": "."})


def to_ascii_input(text: str) -> str:
    """全形英數與中文句點轉成半形；其他字元原樣保留（交給欄位解析報錯）。"""
    return unicodedata.normalize("NFKC", text).translate(_IDEOGRAPHIC_STOPS)


class _Imm32:
    """Windows imm32：把某個視窗的輸入法 context 拿掉／放回。"""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._imm = ctypes.WinDLL("imm32")
        self._user32 = ctypes.WinDLL("user32")
        self._imm.ImmAssociateContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
        self._imm.ImmAssociateContext.restype = wintypes.HANDLE
        self._user32.GetFocus.argtypes = []
        self._user32.GetFocus.restype = wintypes.HWND

    def focused_window(self):
        return self._user32.GetFocus()

    def associate(self, hwnd, himc):
        """設定 hwnd 的輸入法 context（None = 停用），回傳原本的 context。"""
        return self._imm.ImmAssociateContext(hwnd, himc)


def _default_imm():
    if sys.platform != "win32":
        return None
    try:
        return _Imm32()
    except Exception:
        log.warning("imm32 無法載入，英數欄位不停用輸入法", exc_info=True)
        return None


def ascii_entry(entry, variable, *, imm=None) -> None:
    """entry 只收英數：焦點在它身上時停用輸入法；variable 寫入全形字元時轉半形。"""
    if imm is None:
        imm = _default_imm()
    saved: dict = {}

    def on_focus_in(_event=None):
        if imm is None:
            return
        try:
            hwnd = imm.focused_window()
            if hwnd and hwnd not in saved:
                saved[hwnd] = imm.associate(hwnd, None)
        except Exception:
            log.warning("停用輸入法失敗", exc_info=True)

    def on_focus_out(_event=None):
        while saved:
            hwnd, himc = saved.popitem()
            try:
                imm.associate(hwnd, himc)
            except Exception:
                log.warning("恢復輸入法失敗", exc_info=True)

    def on_write(*_args):
        value = variable.get()
        normalized = to_ascii_input(value)
        if normalized != value:
            variable.set(normalized)

    entry.bind("<FocusIn>", on_focus_in, add="+")
    entry.bind("<FocusOut>", on_focus_out, add="+")
    entry.bind("<Destroy>", on_focus_out, add="+")
    variable.trace_add("write", on_write)
