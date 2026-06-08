---
status: accepted
---

# Live 日度風控的時間模型與標價來源

[[日度風控 (Daily Risk Check)]] 的停損 / 跳空判斷，以及 daily report 的部位標價，都必須決定「用哪一盤的收盤」。回測用相鄰兩個歷史日（`date` 執行盤、`prev` 觸發收盤）；但 live 盤前資料只寫到最近一個**已收盤** session（`update_data` 收盤後才寫），[[執行盤 (Execution Session)]]（即將開盤的下一盤）尚未存在於資料中。把 `index[-1]` 誤當執行盤、`index[-2]` 當前收，導致週五跌破停損線、週一卻不出場（grilling 2026-06-07）。

**決定：**

1. **[[前收 (Reference Close)]] = CSV `index[-1]`**（最近官方收盤）。停損比 Reference Close vs 成本、跳空比 Reference Close 當盤 open vs 其前一盤 close（gap-exit 兩個 index 一併往後移一格）。執行盤不對應任何已寫入的歷史列——訂單送進即時的下一盤。
2. **Staleness guard（halt 偏好）**：`index[-1]` 日期 ≠ 預期的最近 NYSE 交易日 → 整個 check 中止、不送任何單、發 Telegram 告警；run 當天非 NYSE 交易日 → no-op；個股缺 Reference Close 列 → 告警 + 跳過該檔（不再靜默跳過）。
3. **報告標價 = Reference Close**：daily report 以最近官方收盤標記部位（EOD mark），IB 報價僅在 CSV 缺列時 fallback。`robust_get_prices`（live 下單取即時 bid/ask）**維持不變**。

## Considered options

- **在 `robust_get_prices` 內全域加 staleness 偵測**（讓報告與下單共用一條修正）：否決——該函式同時餵 live 下單，「這個 IB tick 算不算陳舊」判斷不穩，做錯會污染下單價，對 live 帳戶風險過高。改採「報告自己用 Reference Close 標價、下單路徑不動」的分離法。
- **讓資料管線寫入『當日列』使 live 與 backtest 的 index 對齊**：否決——盤前根本沒有當日收盤，寫部分列會引入更多 race 與半截資料。
- **Staleness 時 best-effort 硬上**（有什麼價就判）：否決——錯位狀態下任何停損判斷都不可信，寧可叫醒人。代價是 stale 資料的那天不會自動停損，需人工或等管線修復。

## Consequences

- 報告 / Panel 的 Excel = 「官方收盤快照」；即時看盤改走 Panel 的 **Refresh IB**（`robust_get_prices` live 路徑）。兩條路徑語意明確分離。
- 報告顯示的價會與停損判斷用的價一致（同一個 Reference Close）。
- 停損會在正確的執行盤觸發（如 CIEN 週五破線 → 週一開盤出場），而非延遲一個交易日。
- guard 採 halt 偏好：資料管線出包的日子，日度風控會「停手 + 告警」而非自行用爛資料下單。
