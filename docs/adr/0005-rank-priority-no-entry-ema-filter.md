---
status: accepted
---

# SP500 進場不套 EXIT_EMA 過濾 — rank 優先（WF 否決進場 EMA 過濾）

2026-06-10 發現 [[SP500]] rotation 的內部矛盾：CIEN 因「跌破EMA50」被 SELL 旗標，同一輪又因動能排名仍高（過濾後 #3）被選回 → net delta 0、續抱，EMA 出場形同無效。直覺修法 = 買入候選套用同一 EMA 條件（`utils.filter_entry_eligible`，legacy portfolio_calc 曾有此過濾）。**Walk-forward 否決了這個修法。**

## WF 證據（2018-07–2026-06，N=8 主跑 + N=7 敏感度，IS:OOS=3:1，fitness=IS Sharpe）

A = 無過濾（rank 優先）、B = 進場 EMA 過濾：

| | N=8 | N=7 |
|---|---|---|
| fold-winner（IS） | **A 5/8** | **A 5/7** |
| OOS NP 對決 | A 5 勝 2 敗 1 平 | 3:3 平 1 平 |
| ΣOOS NP | A +216.8% vs B +194.6% | A +195.3% vs B +172.4% |
| Pardo WFE（A / B） | 1.187 / 1.166 | 1.161 / 1.099 |
| Sharpe WFE（A / B） | 1.203 / 1.169 | 1.049 / 1.036 |
| Winner-chain | Pardo 1.076、Sharpe 1.104 → PASS | Pardo 1.099、Sharpe 0.990 → PASS |

- 兩版 WFE 全部遠超門檻（0.50/0.40），**皆無 overfit 跡象** — 這是「兩個都能用、選較好的」而非「一好一壞」。
- A 的 OOS 優勢集中在 2024-12→2025-09 一個 fold（+19.9pp，佔 22.3pp 總差距的 ~90%）；其餘 7 個 fold 兩版打平（±3pp，B 還贏 2 個）。episodic but real：**急跌的高動能股傾向 V 反彈**，續抱賺得比 EMA 早出避掉的多。
- 連「包含 CIEN 本次崩跌」的 fold（OOS 2025-09→2026-06）也是 A 勝（+59.3% vs +55.6%）。
- N=8 vs N=7 ΔWFE < 0.15 → 對視窗選擇穩健。
- B 僅存的好處：全期 MaxDD −25.4% vs −29.0%（但 Calmar 打平、Sharpe 較差：0.95 vs 1.07）。
- 已知限制：2020-03 崩盤落在 IS 區段，無法被 OOS 直接檢驗。

## 決定

`config.ENTRY_EMA_FILTER = False`（預設 = 無過濾）。「同輪 SELL 又 BUY 回 = 續抱」是**刻意行為**：rank 是第一公民，EMA 出場只在「補位者排名更前」時實質生效。flag、`filter_entry_eligible`、WF 腳本（`robustness/wf_ema_entry_filter.py`）與報告 CSV（`reports/sp500_momentum/`）保留，未來重驗只需翻 flag 重跑 WF。

## Consequences

- 跌破 EMA 但排名仍高的持股（如 CIEN −31%）會**續抱**，直到排名跌出 Top 20 或補位者勝出 — 這是策略設計，不是 bug。要人工強制出場用 `config.BLACKLIST`（constituents 層排除，rotation 不會買回）。
- 回測中同輪 SELL+BUY 以兩筆交易入帳（含雙邊 phantom 手續費）；live 端 combine 後 delta=0 不下單。兩邊已知的微小差異，可忽略。
- 翻動此 flag = 策略變更，必須重跑 WF 過 gate 才能部署。
