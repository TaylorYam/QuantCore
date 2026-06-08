---
status: accepted
---

# 移除 SP500 的固定 20% 停損與 50% 跳空出場

[[SP500]] 動能策略原本有兩個**日度**個股風控:成本基準的 20% 停損、50% 跳空出場(由 backtester 每日檢查、live 由 `sp500_daily_check.py` 執行)。實證 + 結構分析顯示兩者都該移除,個股出場改由**每週三的 EMA50/排名 rotation** 單獨負責。

**決定:** 從**回測與 live 兩邊**移除 20% 停損與 50% 跳空出場。`sp500_daily_check.py` 從此只做熊市 dip-buy 檢查。進場過濾 `SKIP_MAX_GAP_PCT`(跳空 >20% 不買)保留;`STOP_LOSS_PCT` / `GAP_EXIT_PCT` 常數標記 deprecated。

## 證據(2018–2026 回測,含 2020 COVID、2022 熊市)

| 情境 | CAGR | Sharpe | MaxDD | 停損次數 | 跳空次數 |
|---|---|---|---|---|---|
| 含停損/跳空 | +31.59% | 1.07 | −29.2% | 2 | 0 |
| 移除兩者 | +31.64% | 1.07 | **−29.0%** | 0 | 0 |

- 20% 停損 8 年只觸發 **2 次**,其中 1 次(TER 2024-07)隔個調倉日立即被買回 = 白做。移除後績效**幾乎不變、MaxDD 還微幅變好**。
- 50% 跳空出場 **8 年從未觸發**,純死碼。
- 組合 MaxDD 由大盤整體下殺驅動(regime/dip-buy 處理),**不是單一個股 −20%** → 固定停損對回撤無實質保護。

## Considered options

- **保留停損 + weekday-gate**(讓週二 `--mode all` 對 SP500 真正 hold):可消掉 whipsaw 但保留一個回測證實無用的機制,且多一段 code。否決。
- **移除**(本決定):更簡單、數據支持、且從根本消除 whipsaw —— 沒有日度個股賣出 → 週中不動 SP500 → 週二 reconcile 永遠 no-op。採用。

## Consequences

- 個股出場**只剩每週三 rotation(EMA50/排名)**;股票在兩個週三之間可能跌更多才出場(放棄週中急崩的快速出場)。50% 跳空保險本來就從未生效,等於沒損失。
- 消除了 live-only 的「停損 → 週二用舊訊號買回 → 週三再賣」whipsaw(2026-06 CIEN 即此案例);live 行為回到與回測一致。
- [ADR-0003](0003-live-daily-risk-timing-and-marking.md) 的停損 off-by-one 修正就此**失效不再相關**(那條 stop/gap 已不存在);Reference Close 概念仍用於 daily_report 標價與 dip-buy 的資料新鮮度 guard。
- **WFO 驗證待補**:本決定基於 in-sample 對照 + 結構分析;應以 `/wft` walk-forward 確認 out-of-sample 穩健(B 路徑:先上線、WFO 事後補)。
