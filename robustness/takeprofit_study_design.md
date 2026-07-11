# 停利（Take-Profit）研究 — Pre-registered 設計

**日期**: 2026-07-10（grilling 定案）
**狀態**: **CLOSED 2026-07-11** — 停利出場 rejected（[ADR-0010](../docs/adr/0010-sp500-take-profit-exit-rejected.md)）；
副產品「噴出進場封鎖」過 WF gate 成未部署候選。結案報告
[takeprofit_report.md](takeprofit_report.md)。本文件保留為 pre-registration 存證。
**術語**: 見 CONTEXT.md「停利 (Take-Profit)」「噴出 (Blowoff)」詞條與 2026-07-10 三條 flagged ambiguities

## 動機

STX 2025-08 回合：153 進場、峰頂 442、跌破 EMA50 於 367.5 全出 — 高點回吐約 17%。
現行回合 STX 2026-06-03 @940 進場，乖離極大。問題定義（Q1 定案）：**減少贏家股從持有期高點到
rotation 出場的回吐**，非心理安撫、非驗證假說本身。

既有事實（設計約束）：

- ADR-0004：週間出場（停損/跳空）已整個移除 — 8 年回測零貢獻 + 週三 rank 優先買回 whipsaw。
- ADR-0005：rank 優先於 EMA，同輪賣出又買回=續抱是刻意行為。停利若無買回防線即為 no-op。
- 策略已每週經超重賣出（3% threshold + ATR 反比權重）機械式分批停利 — 新規則衡量的是**邊際**貢獻。
- adj_slope 的 r² 已懲罰不穩定上漲；MAX_ADJ_SLOPE=1.5 已在進場端過濾過熱。

## 規則結構（全部 arms 共用）

- **觸發** = 未實現獲利 ≥ X%（相對**本回合首筆進場價**，中途 trim/補倉不重算）AND 噴出條件超標。
- **回合（episode）語意**（2026-07-10 對抗審查修正）：同一週三「賣出又買回」= 續抱
  （ADR-0005，live 端 combine net 為零、不產生訂單），回合**不重設**。歷史 baseline
  有 26 次此類 churn，全集中在噴出贏家（MRNA/FSLR/NVDA/WDC…），不修會系統性低估
  pnl 並污染錨定。唯一實作 `robustness/takeprofit_common.py::EpisodeTrackingMixin`，
  診斷與 arms 必須 import 同源。
- **評估時點** = 只在週三 Rotation，成為 `rotation_sells` 第三個賣出理由（排名/EMA50/停利）。
  「個股出場只由 Rotation 負責」不變量保持成立。
- **買回防線** = 噴出條件（與出場**共用同一門檻値**）對稱封鎖補位買入，比照 MAX_ADJ_SLOPE
  在 `rotation_buys` 的模式（不佔名額）。無 per-ticker state。

## 噴出因子定義

| 因子 | 定義 | 觸發方向 |
|---|---|---|
| 乖離 | adj_close / EMA50(adj_close) − 1（與 selection.py `_EMA50` 同源序列） | > 門檻 |
| Skewness | 調整後日報酬 rolling skew，窗口 sweep {20, 60}（與 compute_skew_score 同源定義） | > 門檻（正偏=噴出型上漲） |
| Kurtosis | 調整後日報酬 rolling kurt（excess/Fisher），窗口 sweep {20, 60} | > 門檻（肥尾=不穩定） |

**單位一致性**（2026-07-10 對抗審查 blocker 修正）：乖離分子分母必須同為調整後序列。
原「raw Close ÷ Adj-Close EMA」寫法在股息回調下產生時間衰減 artifact（STX 2018 實測
+34.5pp）且屬 lookahead（調整因子取決於快照日後的股息）。live 端資料最後一列
Adj==Close，兩寫法收斂，無部署語意分歧。唯一實作
`robustness/takeprofit_common.py::blowoff_factors`。

刻意不採用：ATR 正規化乖離（噴出期 ATR 分母同步膨脹、自我鈍化）、迴歸殘差 z-score（與
adj_slope 的 r² 同一條迴歸線、重疊過高）、週報酬 skew（60 日窗口只剩 n=12 無意義）。

## 門檻取値（先登記，不事後加點）

1. **診斷 pass（不看 P&L）**：抽出歷史上所有「獲利中持股」（週三快照，未實現獲利 > 0）的
   三因子値分布。
2. 門檻登記為該分布的 **P80 / P90 / P95** 對應値 — 三因子嚴格度自動對齊，跨 arm 公平。
3. 獲利前提 X% grid 先登記：**{20%, 40%}**。

## Arms（in-sample 2018-07-01 → **2026-07-09 凍結**）

資料凍結（2026-07-11 補登）：夜間排程每日 04:30 追加新 bar，不凍結則跨日不可重現
（實證：同一 A0 隔日 CAGR 32.12%→31.65%）。全研究（診斷/arms/WF）統一
`takeprofit_common.STUDY_END = "2026-07-09"`，即門檻錨定當時的資料尾。

| Arm | 觸發 | 買回防線 |
|---|---|---|
| A0 baseline | 無（現行策略） | — |
| A1 naive 停利對照 | 獲利 ≥ X%（無結構條件） | 冷卻：當輪+下一輪不得買回（「輪」= 實際執行的 rotation；熊市早退週與休市週三不消耗，2026-07-10 審查修正） |
| A2 乖離 | 獲利 ≥ X% AND 乖離 > Y | 乖離 > Y 對稱封鎖進場 |
| A3 skew | 獲利 ≥ X% AND skew > Y | skew > Y 對稱封鎖進場 |
| A4 kurt | 獲利 ≥ X% AND kurt > Y | kurt > Y 對稱封鎖進場 |

**拆解 arms（僅在某結構 arm 於關卡一勝出後跑，驗歸因）**：
(a) 該結構觸發 + 冷卻 1 週防線（隔離觸發訊號貢獻 vs 對稱封鎖貢獻）；
(b) 只有進場封鎖、無停利出場（隔離封鎖單獨貢獻 — 即「乖離 entry filter」，會擋掉 STX@940 型進場）。

## 成功判準（雙關卡）

- **關卡一（in-sample）**：結構 arm 的 Sharpe **同時** > A0 與 A1，且 MDD 不惡化、CAGR 無明顯
  犧牲。高點回吐分布只作診斷不作優化目標（早出必然壓回吐，平凡解）。另檢查觸發事件是否集中
  於特定高波動族群（乖離未正規化的已知風險）。
- **關卡二（WF gate）**：勝出參數以 wft skill 跑 rolling WF（比照 ADR-0005 的 N=8 + N=7 兩組
  窗口），WFE 過標才議部署。
- 兩關任一不過 → 寫 report 存檔、策略不動（比照 ADR-0004/0006/0007 的 rejected-with-data 模式）。

## Phase 1 結果：門檻登記（2026-07-10，修正後）

診斷 pass 完成（`takeprofit_diagnostic.py`，含對抗審查兩項修正後重跑）。
錨定池 = bull regime 且獲利中的週三持股快照，n=917（116 tickers，2018-07→2026-07）。
引擎保真：CAGR 32.12% / Sharpe 1.060 / MDD −29.00%（包裝零行為改動）。

| 因子 | P80 | P90 | P95 |
|---|---|---|---|
| bias | +0.1669 | +0.2206 | +0.2772 |
| skew20 | +0.7318 | +1.2409 | +1.9414 |
| skew60 | +1.6041 | +2.7049 | +3.5900 |
| kurt20 | +2.2959 | +4.3183 | +6.9605 |
| kurt60 | +7.4072 | +14.7712 | +20.0066 |

以上 15 個門檻値即 Phase 2 arms 的**全部**結構門檻，不事後加點。
已知風險登記：bias 與 atr_pct 的 Spearman rho = +0.302（top-decile ATR 4.7% vs
池平均 3.5%）— 未正規化乖離偏向高波動股，Phase 2 報告需檢查觸發事件的
波動族群集中度。觸發率參考：bias P95 × 獲利≥20% ≈ 3.7% 持股週。

## 範圍外（本研究不碰）

- 每日盤前評估版本（重開 ADR-0004 + ADR-0003 時序問題）— 若週三版過關卡一，可加一支每日評估
  sensitivity arm 量化差距，屆時再議。
- 組合規則（乖離 OR skew OR kurt）— 單因子勝出後再議。
- 任何 live 程式改動。部署（若走到）另依 characterization 標準（OLD/NEW byte-diff + full pytest）。
- ADR — 研究結案時視結果補（adopted 或 rejected-with-data 皆有前例）。
