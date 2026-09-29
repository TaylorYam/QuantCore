"""英數欄位不經過輸入法（#138）— 不開 Tk：假 entry／變數／imm32。"""

from __future__ import annotations

import pytest

from ime_guard import ascii_entry, to_ascii_input


@pytest.mark.parametrize("raw,expected", [
    ("184.4", "184.4"),
    ("１８４．４", "184.4"),
    ("184。4", "184.4"),
    ("184｡4", "184.4"),
    ("ＡＡＰＬ", "AAPL"),
    ("brk.b", "brk.b"),
    ("18ㄡ4", "18ㄡ4"),          # 組字殘留不丟掉，交給欄位解析報錯
])
def test_full_width_input_becomes_ascii(raw, expected):
    assert to_ascii_input(raw) == expected


class FakeEntry:
    def __init__(self):
        self.bindings = {}

    def bind(self, sequence, func, add=None):
        assert add == "+"
        self.bindings.setdefault(sequence, []).append(func)

    def fire(self, sequence):
        for func in self.bindings.get(sequence, []):
            func(None)


class FakeVar:
    def __init__(self, value=""):
        self.value = value
        self.traces = []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        for callback in list(self.traces):
            callback("name", "", "write")

    def trace_add(self, mode, callback):
        assert mode == "write"
        self.traces.append(callback)


class FakeImm:
    def __init__(self, focused=0x1234):
        self.focused = focused
        self.contexts = {focused: "HIMC-1"}
        self.calls = []

    def focused_window(self):
        return self.focused

    def associate(self, hwnd, himc):
        self.calls.append((hwnd, himc))
        previous = self.contexts.get(hwnd)
        self.contexts[hwnd] = himc
        return previous


def test_focus_disables_ime_and_leaving_restores_it():
    entry, var, imm = FakeEntry(), FakeVar(), FakeImm()
    ascii_entry(entry, var, imm=imm)

    entry.fire("<FocusIn>")
    assert imm.contexts[0x1234] is None
    entry.fire("<FocusIn>")                 # 重複 FocusIn 不能蓋掉原本的 context
    entry.fire("<FocusOut>")

    assert imm.contexts[0x1234] == "HIMC-1"
    assert imm.calls == [(0x1234, None), (0x1234, "HIMC-1")]


def test_destroy_while_focused_restores_ime():
    entry, var, imm = FakeEntry(), FakeVar(), FakeImm()
    ascii_entry(entry, var, imm=imm)

    entry.fire("<FocusIn>")
    entry.fire("<Destroy>")

    assert imm.contexts[0x1234] == "HIMC-1"


def test_no_focused_window_or_imm_failure_leaves_entry_usable():
    entry, var, imm = FakeEntry(), FakeVar(), FakeImm(focused=0)
    ascii_entry(entry, var, imm=imm)
    entry.fire("<FocusIn>")
    entry.fire("<FocusOut>")
    assert imm.calls == []

    class BrokenImm(FakeImm):
        def associate(self, hwnd, himc):
            raise OSError("imm32 down")

    entry, var = FakeEntry(), FakeVar()
    ascii_entry(entry, var, imm=BrokenImm())
    entry.fire("<FocusIn>")
    entry.fire("<FocusOut>")
    var.set("１８４．４")
    assert var.get() == "184.4"


def test_variable_writes_are_normalized_once():
    entry, var = FakeEntry(), FakeVar()
    ascii_entry(entry, var, imm=FakeImm())

    var.set("184。4")
    assert var.get() == "184.4"
    var.set("184.4")
    assert var.get() == "184.4"
