# QuantCore 架構重構計畫（歷史參考）

> **⚠️ 本文件為歷史紀錄。** S1–S10 已於 `e12e3e5` 完成並合入 main。
> 剩餘工作見 PLAN_R.md。狀態標示可能與實際完成度不一致——以 git log 和程式碼現況為準。

**來源**：`/improve-codebase-architecture` 稽核報告（2026-04-10）
**狀態**：S1–S10 已完成
**目標**：消除模組全域變數 mutation、God class、複製的輔助函式，讓 codebase 可維護、thread-safe、易於推理

---

## 問題根源（稽核發現的 5 大 smell）

1. **`_apply_config()` + 模組全域變數**（`rebalance_ib`, `daily_report`, `moo_fill_check`）— Thread-unsafe、三份複製、隱藏依賴。SR 訊號日錯位 bug、NAV $0 bug 都源於此。
2. **`tg_send()` 複製 6 份**（5 個 script + Panel 的 `_tg_notify`）— 漂移風險。
3. **`control_panel.py` God class** — 2,479 行、68 個方法，Sync Pos / Gateway Monitor / fetch_ib_portfolio 全部擠在一起。
4. **IB 連線設定複製 5 次** — `ib.connect(...)` + `accountValues` + `positions` 的 pattern 散在 5 個檔案。
5. **策略訊號計算散落 7 個檔案** — `SelectionEngine` / `MarketRegime` 的 setup 每個 callsite 重來一遍。

---

## Dependency Graph

```
S1 (notify.py) ─────────────────────────────────────┐
                                                     │
                                                     ▼
S2 (AccountCtx dataclass) ──┬──► S3 (rebalance_ib)──┬─► S5 (control_panel ctx) ─┬─► S8 (ib_fetcher) ─┬─► S10 (sync_position)
                            │                       │                            │                    │
                            └──► S4 (report/moo/sp) ┤                            ├─► S9 (gateway_mon)  │
                                                    │                            │                    │
                                                    └────► S6 (delete globals)   └────────────────────┘
                                                                                                       ▲
S7 (signals.py) ───────────────────────────────────────────────────────────────────────────────────────┘
```

**平行化機會：**
- S1 / S2 / S7 互相獨立
- S3 / S4 在 S2 完成後可平行
- S8 / S9 在 S5 完成後可平行

---

## 執行狀態

| # | Slice | 狀態 | Commit |
|---|---|---|---|
| S1 | `notify.py` 收斂 tg_send | ✅ 完成（待使用者驗證） | pending |
| S2 | 建立 `AccountCtx` dataclass | ✅ 完成（待使用者驗證） | pending |
| S3 | `rebalance_ib.py` 遷移到 ctx | ✅ 完成（待使用者驗證） | pending |
| S4 | `daily_report` / `moo_fill_check` / `sp500_daily_check` 遷移 | ✅ 完成 + live 驗證（Rosa NAV $6,947.72） | pending |
| S5 | `control_panel.py` 遷移到 ctx | ✅ 完成（待使用者驗證） | pending |
| S6 | 刪除 `_apply_config` + 全域變數 | ✅ 完成（待使用者驗證） | pending |
| S7 | `signals.py` 策略訊號單一入口 | ✅ 完成（待使用者驗證；`portfolio_calc.py` 標為 legacy 例外） | pending |
| S8 | 抽出 `ib_fetcher.py` | ✅ 完成（待使用者驗證） | pending |
| S9 | 抽出 `gateway_monitor.py` | ✅ 完成（待使用者驗證） | pending |
| S10 | 抽出 `sync_position.py` | ✅ 完成（待使用者驗證） | pending |

---

## Slice 明細

---

### S1 — 把 `tg_send` 收斂到 `notify.py`

**目標**：單一 Telegram 通知模組，刪掉 6 份複製。

**Scope**：
- 新檔 `automation/notify.py`：`tg_send(msg, token, chat_id, parse_mode="HTML")`、`tg_send_photo(path, caption, token, chat_id)`
- 替換 `rebalance_ib.py:118`、`daily_report.py:105`、`moo_fill_check.py:71`、`update_data.py:48`、`sinopac_equity_report.py:50`、`control_panel.py._tg_notify`
- 每個 script 改成 `from notify import tg_send`

**Out of scope**：
- 不刪 `TG_TOKEN` / `TG_CHAT_ID` 全域變數（S6 處理）
- 不加 rate-limit / retry

**Acceptance**：
- [ ] `automation/notify.py` 存在
- [ ] `grep -rn "def tg_send" automation/ | wc -l` = **1**
- [ ] `python automation/daily_report.py --account Taylor` 能發出 Telegram
- [ ] Panel 斷線測試仍能發 `🔴 Gateway disconnected`

**Blocked by**：none
**Blocks**：S9

---

### S2 — 建立 `AccountCtx` dataclass（不遷移 callers）

**目標**：引入 immutable 帳戶設定容器。

**Scope**：
- 新檔 `automation/account_ctx.py`：frozen dataclass + `from_name(name, role)` classmethod
- 欄位：`name`, `account`, `host`, `port`, `client_id`, `dry_run`, `order_type`, `outside_rth`, `wait_fill_sec`, `alloc_sr`, `alloc_sp500`, `leverage`, `tg_token`, `tg_chat_id`, `price_feed_port`
- Smoke test：驗證三帳戶都能建立、`leverage == alloc_sr + alloc_sp500`

**Out of scope**：
- 不動任何 caller
- 不刪 `_apply_config`

**Acceptance**：
- [ ] `AccountCtx.from_name('Taylor', ROLE_REBALANCE).account == 'U17800779'`
- [ ] 三帳戶 ctx 建立無 exception
- [ ] `_apply_config` 仍存在未動

**Blocked by**：none
**Blocks**：S3, S4, S5

---

### S3 — `rebalance_ib.py` 改成吃 `AccountCtx`

**目標**：函式從讀全域變數改成讀 ctx 參數（全域變數作為 fallback）。

**Scope**：
- `connect_ib(ctx=None)`, `get_nav(ib, ctx=None)`, `get_positions(ib, ctx=None)`, `get_prices(ib, symbols, ctx=None)`, `compute_sr_weights(ctx=None)`, `compute_sp500_weights(..., ctx=None)`, `combine_targets(..., ctx=None)` 加 ctx 可選參數
- `ctx=None` → fallback 讀全域變數（向後相容）
- `ctx` 有值 → 完全不讀全域變數
- `_apply_config()` 保留（S6 才刪）

**Out of scope**：
- 不改 Panel / 其他 script

**Acceptance**：
- [ ] 舊呼叫 `get_nav(ib)` 仍正常
- [ ] `python automation/rebalance_ib.py --mode sr --account Taylor` dry_run 正常
- [ ] 新增 smoke test：`compute_sr_weights(ctx)` 回傳非空 dict

**Blocked by**：S2
**Blocks**：S5, S6

---

### S4 — 其他 script 遷移到 `AccountCtx`

**目標**：`daily_report` / `moo_fill_check` / `sp500_daily_check` 全部改用 ctx。

**Scope**：
- `daily_report.main()`：`AccountCtx.from_name(args.account, ROLE_REPORT)`
- `moo_fill_check.main()`：同上，`ROLE_MOO_CHECK`
- `sp500_daily_check.run_for_account()`：刪掉 `reb._apply_config(cfg)`，改建 ctx 傳入
- 三個 script 內部函式都加 ctx 參數

**Out of scope**：
- 不刪全域變數（S6）

**Acceptance**：
- [ ] `python automation/daily_report.py --account all` 三帳戶依序正常
- [ ] `python automation/sp500_daily_check.py --account Taylor` 正常
- [ ] `python automation/moo_fill_check.py --account Taylor` 正常
- [ ] 三檔案無 `reb._apply_config()` 呼叫

**Blocked by**：S2
**Blocks**：S6

---

### S5 — `control_panel.py` 遷移到 `AccountCtx`

**目標**：Panel 切換帳戶 / Sync Pos / Refresh 全改用 ctx，不再呼叫 `reb._apply_config()`。

**Scope**：
- `_sync_compute_thread`, `fetch_ib_portfolio`, `_monitor_thread` 改用 ctx
- 移除所有 `reb._apply_config(...)` 呼叫
- 移除對 `reb.HOST` / `reb.PORT` / `reb.ACCOUNT` / `reb.LEVERAGE` / `reb.ALLOC_SR` / `reb.ALLOC_SP500` / `reb.DRY_RUN` / `reb.ORDER_TYPE` / `reb.PRICE_FEED_PORT` 的直接讀取
- `load_config()` 6 處改用 `get_account_config(selected_account)`

**Out of scope**：
- 不拆 Panel 檔案（S8/S9/S10）
- 不刪 `_apply_config`（S6）

**Acceptance**：
- [ ] Panel 啟動正常
- [ ] 三帳戶切換顯示 NAV / 持倉都正確
- [ ] Sync Pos 預覽 + dry_run 下單正常
- [ ] Gateway Status 三卡片 ONLINE
- [ ] `grep -n "reb\._apply_config\|reb\.HOST\|reb\.PORT" automation/control_panel.py` 回傳空

**Blocked by**：S3
**Blocks**：S6, S8, S9

---

### S6 — 刪除 `_apply_config` + 全域變數

**目標**：拔掉所有 `_apply_config()` 和 mutable globals。

**Scope**：
- `rebalance_ib.py` / `daily_report.py` / `moo_fill_check.py` 刪 `_apply_config()` + 全域變數
- 函式簽章的 `ctx=None` default 改成必填
- 確認無 fallback 路徑殘留

**Out of scope**：
- 不動 signals / ib_fetcher / Panel 拆分

**Acceptance**：
- [ ] `grep -n "_apply_config" automation/*.py` 回傳空
- [ ] `grep -n "^global " automation/rebalance_ib.py automation/daily_report.py automation/moo_fill_check.py` 回傳空
- [ ] 三 scheduled script 用 `--account Taylor` 跑過正常
- [ ] Panel 全功能正常

**Blocked by**：S3, S4, S5
**Blocks**：none

---

### S7 — `signals.py` 策略訊號單一入口

**目標**：把散落 7 檔案的策略訊號計算統一。

**Scope**：
- 新檔 `automation/signals.py`：
  - `get_sr_weights(as_of_date=None) -> tuple[dict, Timestamp]`
  - `get_sp500_weights(current_holdings, account_name) -> tuple[dict, Timestamp]`
- 把 `rebalance_ib.compute_sr_weights()` / `compute_sp500_weights()` 的內容搬過去
- `rebalance_ib` 的 compute_* 變成 thin wrapper 或 re-export
- 其他 callers（`portfolio_calc`, `sp500_daily_check`, `control_panel`）改用 `from signals import ...`

**Out of scope**：
- 不改 `strategies/*`

**Acceptance**：
- [ ] `grep -rn "from strategies.sp500_momentum.selection import" automation/` 只剩 signals.py
- [ ] `grep -rn "from strategies.sp500_momentum.market_regime import" automation/` 只剩 signals.py
- [ ] Panel Sync Pos 正常、rebalance dry_run 正常

**Blocked by**：none（建議在 S3 後做）
**Blocks**：S10

---

### S8 — 抽出 `ib_fetcher.py`

**目標**：Panel 的 `fetch_ib_portfolio()` + 所有 script 的 IB 查詢 helper 收斂。

**Scope**：
- 新檔 `automation/ib_fetcher.py`：
  - `connect(ctx: AccountCtx) -> IB`
  - `get_nav(ib, ctx) -> float`
  - `get_positions(ib, ctx) -> dict[str, int]`
  - `fetch_portfolio(ctx) -> PortfolioSnapshot`（含 NAV + positions + prices + debug）
- `PortfolioSnapshot` 取代現在的 loose dict
- Panel 刪掉自己的 `fetch_ib_portfolio`
- `rebalance_ib` / `daily_report` / `moo_fill_check` 刪掉自己的 `connect_ib` / `get_nav` / `get_positions`

**Out of scope**：
- 不動 gateway_monitor / sync_position

**Acceptance**：
- [ ] `automation/ib_fetcher.py` 存在
- [ ] `def connect_ib\|def get_nav\|def get_positions` 在 rebalance_ib / daily_report / moo_fill_check 全消
- [ ] Panel 顯示三帳戶 NAV / 持倉正常
- [ ] 三 script 用 `--account Taylor` 跑都正常

**Blocked by**：S5, S6
**Blocks**：S10

---

### S9 — 抽出 `gateway_monitor.py`

**目標**：Gateway 持久連線監測（~200 行）獨立模組，Panel 只管 UI callbacks。

**Scope**：
- 新檔 `automation/gateway_monitor.py`：
  - `class GatewayMonitor` with `start()`, `stop()`, `is_online(name)`
  - constructor 吃 callbacks：`on_connected`, `on_disconnected`, `on_reminder`
- 內部：3 條持久 IB 連線、backoff 重連、0/10/20 min reminder
- Panel 的 `_start_gateway_monitors` / `_monitor_thread` / `_gw_on_*` / `_gw_reminder` 全部搬走
- Telegram 通知改用 `notify.tg_send`

**Out of scope**：
- 不改 reminder schedule / reconnect backoff

**Acceptance**：
- [ ] `automation/gateway_monitor.py` 存在
- [ ] Panel 開啟三卡片 ONLINE
- [ ] 手動登出某 Gateway → Panel 紅 + Telegram 🔴
- [ ] 10 分鐘後收到 still OFFLINE (10 min)
- [ ] 重連收到 ✅ reconnected
- [ ] Panel 關閉無殭屍連線

**Blocked by**：S1, S5
**Blocks**：none

---

### S10 — 抽出 `sync_position.py`

**目標**：Sync Position 計算 + 下單邏輯（~400 行）從 Panel 拆出。

**Scope**：
- 新檔 `automation/sync_position.py`：
  - `@dataclass SyncOrder`
  - `compute_sync_orders(ctx) -> (orders, summary)`
  - `submit_sync_orders(ib, orders, ctx) -> list[Fill]`
- 使用 S7 `signals.py` 取權重
- 使用 S8 `ib_fetcher.py` 取 NAV / positions / prices
- Panel `_sync_compute_thread` 變 thin wrapper（< 50 行）

**Out of scope**：
- 預覽 dialog UI 保留在 Panel

**Acceptance**：
- [ ] `automation/sync_position.py` 存在
- [ ] Panel `_sync_compute_thread` < 50 行
- [ ] Sync Pos 預覽 UI 顯示正常
- [ ] 三帳戶 dry_run 預覽正確
- [ ] 真實小額 LMT 下單成功

**Blocked by**：S5, S7, S8
**Blocks**：none

---

## 數字檢查（完成後）

| 指標 | 前 | 後 |
|---|---|---|
| `control_panel.py` 行數 | 2,479 | ~1,200 |
| `tg_send` 實作數 | 6 | 1 |
| `_apply_config` 實作數 | 3 | 0 |
| `from strategies.sp500_momentum.selection` import | 5 | 1 |
| `ib.connect(...)` 複製 | 5 | 1 |
| 模組可變全域變數 | ~45 | 0 |

---

## 執行紀律

1. **一次只做一個 slice**，做完跑 acceptance criteria 全通過才 commit
2. 每個 slice 獨立 commit，commit message 格式：`refactor(Sn): <slice 標題>`
3. 每完成一個 slice 跟使用者確認後才進下一個
4. 遇到問題就停下來討論，不私自擴大 scope
5. 完成後更新本 PLAN.md 的執行狀態表
