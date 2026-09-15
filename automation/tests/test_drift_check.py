"""
理論 vs 實際對帳（evaluate_deltas / verify_execution / drift_report_lines）。

需求源頭：2026-06 SkewLS inverse 凍結 35 天無人知 — 用戶要求「一目了然
知道理論與實際的差距是因為什麼原因」。分類器與下單器同一顆腦
（build_order_list = actionable 列的過濾），凍結類 bug 兩道指紋都要當場現形：
  1. excluded_conflict：持倉被誤歸手動卻有策略目標（分類層指紋）
  2. actionable + 零送單覆蓋（verify_execution；執行層指紋）
"""

from __future__ import annotations

import pytest


def _rows(targets, current, other=(), prices=None, nav=100_000):
    from rebalance_plan import evaluate_deltas
    return evaluate_deltas(targets, current, list(other),
                           prices or {}, nav)


def _by_sym(rows):
    return {r.symbol: r for r in rows}


# ── evaluate_deltas 分類矩陣 ─────────────────────────────────────────

def test_category_matrix():
    rows = _by_sym(_rows(
        targets={
            "AAA": {"shares": 50, "strategy": "SR"},      # 對齊
            "BBB": {"shares": 60, "strategy": "SR"},      # 門檻內（1%）
            "CCC": {"shares": 100, "strategy": "SR"},     # 超門檻（5%）
            "DDD": {"shares": 5, "strategy": "SP500"},    # 建倉
            "FFF": {"shares": 10, "strategy": "SkewLS"},  # 無報價
        },
        current={"AAA": 50, "BBB": 50, "CCC": 50, "EEE": 1, "GLD": 7},
        other=("GLD",),
        prices={"AAA": 100.0, "BBB": 100.0, "CCC": 100.0, "DDD": 100.0,
                "EEE": 100.0, "FFF": 0, "GLD": 200.0},
    ))
    assert rows["AAA"].category == "aligned"
    assert rows["BBB"].category == "within_threshold"
    assert "設計內不動作" in rows["BBB"].reason
    assert rows["CCC"].category == "actionable"
    assert "≥" in rows["CCC"].reason
    assert rows["DDD"].category == "actionable" and "建倉" in rows["DDD"].reason
    assert rows["EEE"].category == "actionable" and "清倉" in rows["EEE"].reason
    assert rows["FFF"].category == "no_price"
    assert rows["GLD"].category == "manual" and rows["GLD"].target is None


def test_flip_direction_is_actionable():
    rows = _by_sym(_rows(
        targets={"XLC": {"shares": -5, "strategy": "SkewLS"}},
        current={"XLC": 3},
        prices={"XLC": 100.0},
    ))
    assert rows["XLC"].category == "actionable"
    assert "翻向" in rows["XLC"].reason


def test_excluded_conflict_is_freeze_fingerprint():
    """凍結事故分類層指紋：持有 + 被歸手動 + 卻有策略目標 → conflict。"""
    rows = _by_sym(_rows(
        targets={"REW": {"shares": 263, "strategy": "SkewLS"}},
        current={"REW": 85},
        other=("REW",),                      # 誤分類（2026-06 實況）
        prices={"REW": 20.0},
    ))
    assert rows["REW"].category == "excluded_conflict"
    assert "分類異常" in rows["REW"].reason
    assert rows["REW"].target == 263 and rows["REW"].current == 85


def test_build_order_list_equals_actionable_rows():
    """下單器 = 分類器 actionable 列 — 同一顆腦驗證。"""
    from rebalance_plan import build_order_list
    targets = {
        "AAA": {"shares": 50, "strategy": "SR"},
        "CCC": {"shares": 100, "strategy": "SR"},
        "REW": {"shares": 263, "strategy": "SkewLS"},
    }
    current = {"AAA": 50, "CCC": 50, "REW": 85}
    prices = {"AAA": 100.0, "CCC": 100.0, "REW": 20.0}
    orders = build_order_list(targets, current, ["REW"], prices, 100_000)
    rows = _rows(targets, current, ("REW",), prices)
    actionable = [r.symbol for r in rows if r.category == "actionable"]
    assert [o.symbol for o in orders] == actionable == ["CCC"]
    # conflict 列不下單（由對帳報表警示，非默默下單）
    assert all(o.symbol != "REW" for o in orders)


# ── verify_execution（送單覆蓋）──────────────────────────────────────

def _actionable_row(sym="REW", delta=178):
    from rebalance_plan import DeltaRow
    return DeltaRow(sym, "actionable", f"漂移 3.6% ≥ 3% 門檻",
                    85 + delta, 85, delta, 20.0, 0.036, "SkewLS", 0.03)


def test_verify_uncovered_is_red_flag():
    """執行層凍結指紋：該動、卻一張單都沒送。"""
    from rebalance_plan import verify_execution
    out = verify_execution([_actionable_row()], placed_orders=[])
    assert out[0].category == "actionable"
    assert "未送出任何對應單" in out[0].reason


def test_verify_exact_coverage_becomes_submitted():
    from rebalance_plan import verify_execution
    placed = [{"symbol": "REW", "action": "BUY", "qty": 178}]
    out = verify_execution([_actionable_row()], placed)
    assert out[0].category == "submitted"
    assert "待成交" in out[0].reason


def test_verify_partial_coverage_keeps_actionable():
    from rebalance_plan import verify_execution
    placed = [{"symbol": "REW", "action": "BUY", "qty": 100}]
    out = verify_execution([_actionable_row()], placed)
    assert out[0].category == "actionable"
    assert "部分覆蓋" in out[0].reason and "+78" in out[0].reason


def test_verify_sell_direction_coverage():
    from rebalance_plan import verify_execution
    row = _actionable_row(sym="ERY", delta=-145)
    placed = [{"symbol": "ERY", "action": "SELL", "qty": 145}]
    out = verify_execution([row], placed)
    assert out[0].category == "submitted"


def test_verify_leaves_non_actionable_untouched():
    from rebalance_plan import verify_execution
    rows = _rows(targets={"AAA": {"shares": 50, "strategy": "SR"}},
                 current={"AAA": 50}, prices={"AAA": 100.0})
    out = verify_execution(rows, placed_orders=[])
    assert out[0].category == "aligned"


# ── drift_report_lines 渲染 ──────────────────────────────────────────

def test_report_red_section_lists_details():
    from rebalance_plan import drift_report_lines, verify_execution
    rows = verify_execution([_actionable_row()], [])
    text = "\n".join(drift_report_lines(rows))
    assert "🔴 需要注意 1 檔" in text
    assert "REW [SkewLS]: 應 263 實 85（差 +178 股, 3.6% NAV）" in text
    assert "未送出任何對應單" in text


def test_report_all_green_summary():
    from rebalance_plan import drift_report_lines
    rows = _rows(
        targets={"AAA": {"shares": 50, "strategy": "SR"},
                 "BBB": {"shares": 60, "strategy": "SR"}},
        current={"AAA": 50, "BBB": 50, "BTC": 3},
        other=("BTC",),
        prices={"AAA": 100.0, "BBB": 100.0, "BTC": 30.0},
    )
    text = "\n".join(drift_report_lines(rows))
    assert "🔴" not in text
    assert "🟡 門檻內漂移 1 檔（最大: BBB 1.0%）" in text
    assert "⚪ 手動排除: BTC" in text
    assert "✅ 對齊 1 檔，無異常" in text


def test_report_submitted_line():
    from rebalance_plan import drift_report_lines, verify_execution
    rows = verify_execution(
        [_actionable_row()],
        [{"symbol": "REW", "action": "BUY", "qty": 178}])
    text = "\n".join(drift_report_lines(rows))
    assert "🕐 已送單待成交 1 檔: REW+178" in text
    assert "🔴" not in text


# ── 同策略合計偏離升級（🟠 aggregate）────────────────────────────────

def _yvonne_residual_rows():
    """2026-07-06 Yvonne 實況：凍結遺留 — 單檔皆 < 3%、SkewLS 合計 ~9% NAV。"""
    nav = 108_462
    targets = {
        "REW": {"shares": 263, "strategy": "SkewLS"},
        "ERY": {"shares": 171, "strategy": "SkewLS"},
        "REK": {"shares": 91, "strategy": "SkewLS"},
        "SDP": {"shares": 127, "strategy": "SkewLS"},
        "SMN": {"shares": 44, "strategy": "SkewLS"},
        "XLC": {"shares": 24, "strategy": "SkewLS"},
        "XLI": {"shares": 51, "strategy": "SR"},
    }
    current = {"REW": 85, "ERY": 316, "REK": 209, "SDP": 63, "SMN": 66,
               "XLC": 2, "XLI": 62}
    prices = {"REW": 12.6, "ERY": 13.2, "REK": 15.6, "SDP": 22.0,
              "SMN": 20.1, "XLC": 108.0, "XLI": 190.0}
    return _rows(targets, current, (), prices, nav=nav)


def test_aggregate_drift_escalates_to_orange():
    """凍結遺留的形狀：單檔皆綠、同策略合計 ≥6% → 🟠 必須現形。"""
    from rebalance_plan import drift_report_lines
    rows = _yvonne_residual_rows()
    assert all(r.category == "within_threshold" for r in rows), \
        "前提：每一檔都在門檻內（隱形偏離場景）"
    text = "\n".join(drift_report_lines(rows))
    assert "🟠 SkewLS 合計偏離" in text
    assert "排程不會自動修" in text and "Sync Pos" in text
    assert "Σ 偏離 by 策略: SkewLS" in text
    assert "無異常" not in text, "有 🟠 時不得說無異常"


def test_aggregate_below_warn_shows_sigma_only():
    from rebalance_plan import drift_report_lines
    rows = _rows(
        targets={"BBB": {"shares": 60, "strategy": "SR"}},
        current={"BBB": 50},
        prices={"BBB": 100.0},
    )   # SR 合計 1% < 6%
    text = "\n".join(drift_report_lines(rows))
    assert "🟠" not in text
    assert "Σ 偏離 by 策略: SR 1.0%" in text
    assert "無異常" in text


def test_aggregate_excludes_submitted():
    """已送單（開盤即修）不計入 standing 合計 — 送單當晚不得誤報 🟠。"""
    from rebalance_plan import strategy_drift_totals, verify_execution
    rows = verify_execution(
        [_actionable_row(sym=s, delta=178) for s in ("REW", "ERY", "REK")],
        [{"symbol": s, "action": "BUY", "qty": 178}
         for s in ("REW", "ERY", "REK")])
    assert strategy_drift_totals(rows) == {}


def test_strategy_totals_grouping():
    from rebalance_plan import strategy_drift_totals
    rows = _yvonne_residual_rows()
    totals = strategy_drift_totals(rows)
    assert set(totals) == {"SkewLS", "SR"}
    assert totals["SkewLS"] == pytest.approx(
        sum(r.drift_pct for r in rows if r.strategy == "SkewLS"))
    assert totals["SkewLS"] >= 0.06 > totals["SR"]


# ── 6/5 凍結事故重演（end-to-end regression）─────────────────────────

def test_freeze_incident_would_have_alarmed_same_night():
    """2026-06-05 觸發日重演：sector 腿有送單、inverse 腿的單被丟 —
    對帳段必須當晚給 🔴，而不是 35 天後人肉發現。"""
    from rebalance_plan import drift_report_lines, verify_execution

    targets = {
        "XLF": {"shares": 56, "strategy": "SkewLS"},    # sector 腿
        "REW": {"shares": 61, "strategy": "SkewLS"},    # inverse 腿（被丟）
    }
    current = {"XLF": 46, "REW": 20}
    prices = {"XLF": 40.0, "REW": 12.0}
    rows = _rows(targets, current, (), prices, nav=10_000)
    # sector 腿有單、inverse 腿零單（凍結實況）
    placed = [{"symbol": "XLF", "action": "BUY", "qty": 10}]
    text = "\n".join(drift_report_lines(verify_execution(rows, placed)))

    assert "🔴" in text and "REW" in text and "未送出任何對應單" in text
    assert "🕐" in text and "XLF" in text


# ── drift_alert_lines（#26：TG 只帶警報，🟠 只附帶）──────────────────

def test_alert_lines_red_is_alarm():
    from rebalance_plan import drift_alert_lines, drift_report_lines, verify_execution
    rows = verify_execution([_actionable_row()], [])
    alarms, advisories = drift_alert_lines(rows)
    assert alarms[0] == "🔴 需要注意 1 檔:"
    assert "REW [SkewLS]: 應 263 實 85（差 +178 股, 3.6% NAV）" in alarms[1]
    assert advisories == []
    # 與 log 報表同一套文字
    assert all(ln in drift_report_lines(rows) for ln in alarms)


def test_alert_lines_orange_is_advisory_only():
    from rebalance_plan import drift_alert_lines
    alarms, advisories = drift_alert_lines(_yvonne_residual_rows())
    assert alarms == []
    assert len(advisories) == 1 and advisories[0].startswith("🟠 SkewLS 合計偏離")


def test_alert_lines_no_price_is_alarm():
    from rebalance_plan import drift_alert_lines
    rows = _rows(targets={"FFF": {"shares": 10, "strategy": "SkewLS"}},
                 current={}, prices={"FFF": 0})
    alarms, _ = drift_alert_lines(rows)
    assert alarms == ["⚠️ 無報價跳過: FFF"]


def test_alert_lines_quiet_when_submitted_yellow_manual_aligned():
    from rebalance_plan import drift_alert_lines, verify_execution
    rows = verify_execution(
        _rows(targets={"AAA": {"shares": 50, "strategy": "SR"},
                       "BBB": {"shares": 60, "strategy": "SR"},
                       "CCC": {"shares": 100, "strategy": "SR"}},
              current={"AAA": 50, "BBB": 50, "CCC": 50, "BTC": 3},
              other=("BTC",),
              prices={"AAA": 100.0, "BBB": 100.0, "CCC": 100.0, "BTC": 30.0}),
        [{"symbol": "CCC", "action": "BUY", "qty": 50}])
    assert drift_alert_lines(rows) == ([], [])
