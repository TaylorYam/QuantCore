"""哪個 Gateway 視窗是登入視窗（#124）。

測資是 2026-09-29 live 機器上實際的視窗標題：Taylor 的 Gateway 語言是英文
（IBKR Gateway），Rosa、Yvonne 是繁體中文（IBKR網關）。舊版只認標題裡的
"gateway"，中文的兩個永遠找不到。
"""
from __future__ import annotations

import pytest

from gateway_start_win import is_login_window_title


@pytest.mark.parametrize("title", ["IBKR Gateway", "IBKR網關", "IBKR网关"])
def test_login_window_is_recognised_in_every_gateway_language(title):
    assert is_login_window_title(title)


@pytest.mark.parametrize("title", ["", "theAwtToolkitWindow", "Default IME", "MSCTFIME UI"])
def test_gateway_helper_windows_are_not_the_login_window(title):
    assert not is_login_window_title(title)
