# QuantCore 架構重構 — Round 2 計畫（S10 之後）

**來源**：S1-S10 完成後的剩餘工作清單（審視 audit report + session 過程發現的問題）
**狀態**：待執行
**目標**：修掉 S1-S10 來不及處理的 bug 與 cleanup、加入測試保護、完成 Panel 瘦身最後一步

---

## 現況

S1-S10 已 commit `e12e3e5` 推到 origin/main。核心架構改好了：
- 零 module-level mutable globals
- AccountCtx / signals / ib_fetcher / gateway_monitor / sync_position / notify 都就位
- Panel 從 2,479 → 2,119 行

**但還有殘留工作**：1 個 production bug、4 個 cleanup、測試、Panel 完整瘦身、2 個決策題。

---

## Phase R1 — Bug fix + 低垂果實（< 1 小時，零風險）

**目標**：修掉 production bug、刪 dead code、消除 drift 隱患、完成 S6 的尾巴

| # | 項目 | 估時 | 類別 |
|---|---|---|---|
| R1.1 | 修 `equity_chart.py` `'>=' not supported between instances of 'float' and 'str'` | 5 min | 🐛 Bug |
| R1.2 | 刪 `automation/_audit_test.py` (dev dead code) | 30 sec | 🧹 |
| R1.3 | 統一 `SR_ETFS` 常數：rebalance_ib / control_panel / daily_report 3 份 → 改用 `ib_fetcher.SR_ETFS` | 15 min | 🧹 |
| R1.4 | Panel 5 處 `load_config()` fallback → 改用 `get_account_config(selected)`（完成 S6 的尾巴） | 20 min | 🧹 |

**Acceptance**：
- [ ] equity_chart 在 daily_report 時不再 warning
- [ ] `grep -rn "_audit_test" automation/` → 空
- [ ] `grep -rn "^SR_ETFS =" automation/` 只剩 1 處（ib_fetcher.py）
- [ ] `grep -n "load_config()" automation/control_panel.py` 只剩 `def load_config`

**Commit**：`chore: post-S10 cleanup (equity chart fix, SR_ETFS dedup, load_config collapse)`

---

## Phase R2 — pytest smoke tests（1-2 小時）

**目標**：為最容易壞的核心函式建立測試基底，保護 R3 的 Panel refactor

新建 `automation/tests/`：

| 測試範圍 | 函式 | 測試類型 |
|---|---|---|
| `test_account_ctx.py` | `AccountCtx.from_name(name, role)` | 3 帳戶 × 5 roles：欄位正確、frozen、client_id 唯一 |
| `test_signals.py` | `get_sr_weights()` / `get_sp500_universe()` / `get_market_regime_state()` | 回非空、權重加總≈1、universe 含 SPY/SSO |
| `test_combine_targets.py` | `rebalance_ib.combine_targets(nav, sr, sp, prices, ctx)` | valid/invalid price、NaN 防護、alloc 計算 |
| `test_sync_position.py` | `sync_position._build_order_list` | REBALANCE_THRESHOLD 過濾、清倉例外、建倉例外 |
| `test_notify.py` | `notify.tg_send` | 空 token 靜默、網路錯誤不 raise |
| `test_account_utils.py` | `update_account_config` | write-back、不汙染其他帳戶 |

**Acceptance**：
- [ ] `pytest automation/tests/` 全綠
- [ ] ~20-30 個測試
- [ ] 不依賴 IB 連線、不依賴 network

**Commit**：`test: add smoke/unit tests for core modules`

---

## Phase R3 — Panel 瘦身最後一步（2-3 小時）

**目標**：完成 S10 的 aspirational goal — Panel → ~1,400 行 thin controller

| # | 項目 | Panel 行數影響 | 風險 |
|---|---|---|---|
| R3.1 | 抽出 `_show_sync_preview` → 新檔 `sync_preview_window.py`（`SyncPreviewWindow` class） | **−252** | 中（UI 改動最大） |
| R3.2 | 抽出 Portfolio Overview UI → `panel/portfolio_view.py` | −150 | 低 |
| R3.3 | 抽出 Task cards / Trades list | −180 | 低 |
| R3.4 | 抽出 Strategy Params sliders + Dry-run toggle | −80 | 低 |

做完 R3.1 後 Panel ≈ 1,870 行；做到 R3.4 ≈ 1,400 行。

**依賴**：R2 的測試先到位，這樣 refactor 不用每次都手動測三帳戶 Sync Pos。

**Acceptance**：
- [ ] Panel `wc -l` < 1,500 行
- [ ] `SyncPreviewWindow` class 有 public `show()` method
- [ ] 手動驗證：Panel 啟動 / 切帳戶 / Sync Pos 預覽 / Cancel / Confirm → 全部正常
- [ ] R2 的測試全綠

**Commit**：每個 R3.x 各一個 commit（`refactor(panel): extract xxx`）

---

## Phase R4 — 決策題（已定調）

**R4.1 — Gateway Monitor 首次連線 race** ✅ Option A

修法：`gateway_monitor.py` 的 `BACKOFF_SCHEDULE` 從 `[10, 30, 60, 60]` 改為
`[3, 10, 30, 60, 60]`，首次失敗 3 秒後快速重試，處理 Panel 啟動時 Gateway
正在 ready 中的 race。

**R4.2 — `portfolio_calc.py` 774 行 legacy** ✅ Option C — 保留

原因：
- 還在用（`QuantCore_Calc.vbs` 啟動獨立 GUI 計算機）
- 大部分是 UI code，不是重複的計算邏輯
- `get_sp500_weights()` 回傳的 UI metadata（drop 原因、rank）跟 signals
  API 的純 weight 簽章不同，若要整合需要先為 signals 加新 API
- 下次 audit 會再提醒，但不影響主流程

---

## 執行紀律（與 S1-S10 相同）

1. 一次只做一個 phase，做完跑 acceptance criteria 全通過才 commit
2. 每個 phase 或每個 R3 的抽取獨立 commit
3. 每完成一個 phase 跟使用者確認後才進下一個
4. 遇到問題就停下來討論，不私自擴大 scope

---

## 執行狀態

| Phase | 項目 | 狀態 | Commit |
|---|---|---|---|
| R1 | post-S10 cleanup | ✅ 完成 | 663452f |
| R2 | smoke tests (52 tests) | ✅ 完成 | a0677ed |
| R3.1 | sync preview window | ✅ 完成（Panel −224 行） | e3e7453 |
| R3.2 | portfolio view | ✅ 完成 | 290e4a0 |
| R3.3 | task cards + trades view | ✅ 完成 | 290e4a0 |
| R3.4 | strategy params view | ✅ 完成 | 290e4a0 |
| R4.1 | Gateway backoff fast-retry | ✅ 完成 | pending |
| R4.2 | portfolio_calc.py legacy | ✅ 保留 (無 code 改動) | — |

**R3.2-3.4 合併結果**：Panel 1,895 → 1,392 行（−503 行，超越 PLAN_R 的 <1,500 目標）。
抽出 3 個檔案：
- `portfolio_view.py` (364 行) — NAV 卡 + 持倉列表 + 圓餅圖
- `panel_task_cards.py` (246 行) — TaskCard + TradesView
- `strategy_params_view.py` (242 行) — Dry Run + ETF/Stocks sliders + Save
