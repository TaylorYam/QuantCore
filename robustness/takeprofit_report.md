# SP500 停利研究 — 結案報告

**日期**: 2026-07-11（研究 2026-07-10 grilling 定案 → 07-11 結案）
**結論**: 停利出場 **rejected-with-data**；意外發現「噴出進場封鎖」通過 WF gate，成為待決部署候選
**設計**: [takeprofit_study_design.md](takeprofit_study_design.md)（pre-registered，門檻/grid 未事後擴充）
**資料**: 2018-07-01 → 2026-07-09 凍結（`takeprofit_common.STUDY_END`）
**ADR**: [ADR-0010](../docs/adr/0010-sp500-take-profit-exit-rejected.md)

## TL;DR

WDC/STX 高點回吐引發的原始命題——「乖離過大 = 脫離穩定上漲 → 停利」——被
數據**全面否決**：乖離停利全 grid 輸 baseline（最壞 CAGR 12.5% vs 32.1%），
skew/kurt 停利出場在拆解隔離下亦輸 baseline。形式上過關卡一的 kurt60/skew60
arms，其超額報酬**全部來自對稱進場封鎖的副產品**：「60 日高階動差超標的
噴出股不佔補位名額」。該封鎖規則單獨測試 in-sample Sharpe 1.256 vs 1.060，
並通過 ADR-0005 同協議 WF gate（kurt60 與 skew60、N=8 與 N=7 全 PASS）。

## Phase 1 — 診斷（門檻錨定，不看 P&L）

錨定池 = bull regime 獲利中持股週三快照 n=917（116 tickers）。
門檻登記於設計文件（bias P80/90/95 = 0.167/0.221/0.277 等 15 値）。
對抗審查修正兩項會污染錨定的缺陷：(1) 續抱 churn 錯誤重設回合進場價
（26 次，全在噴出贏家）；(2) 乖離 raw Close ÷ Adj-Close EMA 的股息
artifact（STX 2018 實測 +34.5pp，屬 lookahead）— 修正後乖離門檻下修
8~10pp。已知風險：bias 與 atr_pct Spearman +0.302（高波動族群傾斜）。

## Phase 2 — In-sample arms（關卡一）

33 arms（baseline + naive×2 + 三因子×15 組合），全部含：獲利前提（回合
首筆進場價 +20%/40%）、只在週三 rotation 評估、噴出條件對稱封鎖進場、
naive 用冷卻 2 輪。重點結果（完整表 `output/takeprofit/arms/summary.csv`）：

| Arm | Sharpe | CAGR | MDD | tp_sells |
|---|---|---|---|---|
| A0 baseline | 1.060 | 32.1% | −29.0% | 0 |
| A1 naive x20 | 1.071 | 31.2% | −30.1% | 48 |
| A2 乖離（六組全部） | 0.43~0.98 | 12.5~28.1% | 最壞 −44.9% | 9~29 |
| A4 kurt60_P90_x40（關卡一形式冠軍） | 1.246 | 37.9% | −24.0% | **1** |
| A4 kurt60_P95（零出場仍勝 baseline） | 1.086 | 32.6% | −28.2% | **0** |

紅旗即歸因線索：冠軍 arm 8 年只停利 1~2 次（META 2023-07 +25%、PLTR
2024-11 +41%），且兩筆皆為**壞出場**（META 出場後一年 +53%；PLTR 出場價
53.44 之後 26 週內最低 +5%、一年 +191%）；P95 arm 零出場仍勝 baseline
→ 超額報酬不可能來自停利出場。

## 拆解（預登記 decomposition）

| Arm | Sharpe | CAGR | MDD | 意義 |
|---|---|---|---|---|
| **D1_block_kurt60_P90**（純封鎖，零出場） | **1.256** | **38.2%** | **−24.0%** | 比完整 arm 更好 |
| D1_block_skew60_P90 | 1.246 | 38.0% | −24.0% | 同上 |
| D2_exit（純停利出場+冷卻，4 組） | 1.017~1.054 | 30.8~31.9% | −29~−30% | **全部 ≤ baseline** |

歸因結論：**停利出場貢獻 ≤ 0**；全部 edge 來自進場封鎖。封鎖事件
44 次 / 21 tickers / 平均分布 8 年（CMG、QCOM、ALGN、ANET 2022-01
熊市前夕、META、EXPE、PLTR…），非單一事件運氣（`blocks_*.csv`）。

## 關卡二 — WF gate（ADR-0005 同協議）

`wf_takeprofit_block.py`：Pardo rolling，N=8 主跑 + N=7 敏感度，IS:OOS=3:1，
fitness=IS Sharpe，**每折門檻以 IS 窗內診斷快照重錨 P90**（無 lookahead；
快照來自 baseline run 與規則無關）。PASS gate：pardo_chain>0.50 且
sharpe_chain>0.40。

| 因子 | 配置 | pardo_chain | sharpe_chain | OOS np 勝負 (B:A) | ΣOOS np B vs A | STATUS |
|---|---|---|---|---|---|---|
| kurt60 | N=8 | 1.206 | 1.141 | 5:1（2 平） | +249.8% vs +219.2% | **PASS** |
| kurt60 | N=7 | 0.894 | 0.880 | 5:2 | +212.8% vs +186.2% | **PASS** |
| skew60 | N=8 | 1.325 | 1.231 | 6:1 | +266.8% vs +219.2% | **PASS** |
| skew60 | N=7 | 0.923 | 0.912 | 5:2 | +216.2% vs +186.2% | **PASS** |

每折重錨門檻在 10.3~22.5（kurt60）漂移仍穩定勝出 — 對門檻値不敏感。
細節 CSV：`reports/sp500_momentum/v01_sp500mom_tpblock_{kurt60,skew60}_WF_N{8,7}_PASS_2026-07-11.csv`。

## 誠實聲明（selection 與極限）

- kurt60/skew60 是 15 個 in-sample 組合挑出的（factor 選擇 post-hoc）；
  WF 的每折重錨已消解「門檻値」的 selection，但「因子家族」仍是挑過的。
  兩個相關因子同步 PASS 降低了因子特異的疑慮，無法完全消除。
- In-sample +6.1pp CAGR 幾乎必然高估；WF OOS 差距換算約 +3~5pp/yr。
- 乖離（bias）作為封鎖因子未測 WF —— in-sample 連關卡一都沒過，不追。
- 封鎖規則的機制解讀：kurt60>P90 的候選多為「剛暴漲的噴出股」，rank
  優先會在 EMA 賣出同輪買回（ADR-0005 續抱）或以補位名額追高；封鎖使
  這些進場延後到分布正常化。與 MAX_ADJ_SLOPE（斜率過熱）互補：一個擋
  「漲太快」，一個擋「漲相不穩」。

## 附錄 — 進場濾網 2×2 因子矩陣（2026-07-11，`takeprofit_filter_matrix.py`）

| 格 | adj_slope≥1.5 | kurt60>P90 | CAGR | Sharpe | MDD | 封鎖數 |
|---|---|---|---|---|---|---|
| F0 | ✗ | ✗ | 34.15% | 1.0635 | −26.76% | 0 |
| F1（=現行 live） | ✓ | ✗ | 32.12% | 1.0604 | −29.00% | 0 |
| F2 | ✗ | ✓ | 37.23% | 1.1730 | −24.79% | 40 |
| F3（=D1 冠軍） | ✓ | ✓ | **38.23%** | **1.2564** | **−23.96%** | 42 |

邊際貢獻（Sharpe）：kurt 單獨 +0.110（F2−F0）、在 adj_slope 之上 +0.196（F3−F1）；
adj_slope 單獨 **−0.003**（F1−F0，in-sample 甚至 MDD 惡化）、在 kurt 之上 +0.083（F3−F2）。
**正交互作用**：兩濾網合用 > 任一單用。注意：WF 證據只涵蓋 F3 vs F1（現行 live 加不加
kurt）；F2/F0 的比較與「移除 adj_slope」皆為 in-sample 觀察，未過 WF，不構成動
MAX_ADJ_SLOPE 的依據。F1/F3 數字精確複現 A0/D1 — 矩陣腳本保真。

## 處置

1. **停利出場：結案不採用**（ADR-0010）。個股出場維持「只由週三
   Rotation 負責」（ADR-0004 不變量）。
2. **噴出進場封鎖：已部署（2026-07-11，[ADR-0011](../docs/adr/0011-sp500-blowoff-entry-block.md)）**。
   Characterization 全過：flag-OFF 回測 706 trades 對 A0 byte-identical、
   9/9 preview SHA256 相同、pytest 綠；flag-ON 回測 692 trades 對研究
   D1 arm byte-identical（生產 ≡ 研究實作）。回滾 = `BLOWOFF_ENTRY_BLOCK`
   翻 False 一行。
3. 研究基礎設施留存可重跑：`takeprofit_{common,diagnostic,arms}.py`、
   `wf_takeprofit_block.py`（`--factor kurt60|skew60`）— 皆已釘
   `BLOWOFF_ENTRY_BLOCK=False` 防部署後 flag 污染研究語意。
