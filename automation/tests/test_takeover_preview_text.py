"""takeover_preview_text：交給策略接管視窗的按鈕、確認與結果文字（Tk／Qt 共用）。"""

from strategy_takeover import TakeoverPreview

import takeover_preview_text as text


def _preview(current=10.0, target=0):
    return TakeoverPreview(account_name="Rosa", symbol="XYZ", current=current,
                           manual=current, target=target, parts=(("SP500", target),),
                           pause_reason="r", price=100.0, nav=100000.0, signal_dates="d")


def test_window_title():
    assert text.window_title("Rosa", "XYZ") == "交給策略接管 — Rosa XYZ"


def test_confirm_label_and_prompt_for_an_order():
    p = _preview(current=10.0, target=0)
    assert text.confirm_label(p) == "確認接管並送出 SELL 10"
    assert text.confirm_prompt(p) == (
        "[Rosa] XYZ\n送出 SELL 10 股 XYZ（LMT，只這一檔）\n"
        "成交並與 IB 持倉對上後才解除暫停。確定？")
    assert text.start_log_line(p) == "[>] 交給策略接管 [Rosa] XYZ..."


def test_confirm_label_and_prompt_when_already_on_target():
    p = _preview(current=7.0, target=7)
    assert text.confirm_label(p) == "確認接管"
    assert "不需下單，直接把這檔交給策略" in text.confirm_prompt(p)


def test_result_title():
    assert text.result_title("COMPLETED") == "✅ 接管完成"
    assert text.result_title("PENDING") == "🕐 已送出，等成交與對帳"
    assert text.result_title("FAILED") == "❌ 接管未完成"
    assert text.result_title("DRY_RUN") == "[DRY RUN] 沒有送單"
    assert text.result_title("WEIRD") == "WEIRD"
