# 碎股 vs 整數股 Sizing 研究報告

**日期**:2026-07-05
**腳本**:[frac_sizing_study.py](frac_sizing_study.py)(三臂回測掃描)、[frac_static_analysis.py](frac_static_analysis.py)(live log 實證)
**輸出**:`robustness/output/frac_sizing/`(results.json、curves/、static_*.csv、PNG)
**背景**:CONTEXT.md 詞條 [[理論部位 (Target Notional)]]、[[整數執行稅 (Integer Execution Tax)]];grilling 2026-07-05 定案之純研究輪

---

## TL;DR

1. **病是真的,但集中在「小 sleeve × 高價股」的交集。** 8 年回測(2018-07 → 2026-07)的整數執行稅:起始 $2k 臂高達 **−2.85pp CAGR**、$4k 臂 −1.53pp;Taylor 錨點($8,239)−0.61pp;Rosa/Yvonne 錨點已落入 ±0.3pp 的路徑噪音底。
2. **「今天的 Taylor」比 8 年平均嚴重得多。** Live 實測(2026-04~07,11 次 rebalance):Taylor SP500 sleeve 平均只部署 **89.1%**(最低 82.1%),MU 平均低配 −24%、最差 −45%——比回測 $2k 臂的第 10 百分位(91.4%)還差。原因:回測的起始資金會複利長大稀釋病情($8,239 起步 8 年後滾到 $77k),而 live 的 Taylor **現在就是 $8.2k**,又撞上 2026 年超高價 momentum cohort(MU $1,084 / STX $918 / WDC $604)。**若此 cohort 持續,Taylor 的當前稅率對應回測 $2k–$4k 臂區間:每年 1.5–2.9pp。**
3. **`round()` 一行改動免費拿走大部分好處。** $2k 臂:floor→round 收回 +2.21pp(= floor→frac 差距的 78%);$8k 以上 round ≈ frac 統計不可分。round 不需要 IBKR 碎股權限、不動 MOO 單型、不碰 API 風險。
4. **whipsaw 假說不成立(修正訪談時的推測)。** 三臂的 10 日內賣後買回次數 17–22 次,無系統性差異——3% REBALANCE_THRESHOLD 已有效抑制 0↔1 股震盪。
5. 碎股(frac)仍是結構性正解(權重誤差壓到 4.4pp 的門檻底線、部署率 99.1%),但 live 化被兩道閘卡住:**MOO 不收碎股(自家帳戶實測證實)**、**API 支援度官方文件矛盾(待 $2 實單仲裁)**。

---

## 1. 實驗設計

| 設計項 | 內容 |
|---|---|
| 三臂 | `floor` = int()(現行 live SR/SP500 語意)/ `round` = int(round())/ `frac` = 0.0001 股(IBKR 記錄下限) |
| 單一變因 | 三臂的 3% threshold、MIN_BUY、訊號、選股、期間完全相同,只差取整粒度 |
| 資金階梯 | $2k–$256k 倍增 + $1M 基準 + 三帳戶 SP500 sleeve 錨點(Taylor $8,239 / Rosa $11,349 / Yvonne $27,116 = 2026-07-01 NAV × alloc_sp500) |
| 佣金 | 主結果 IBKR Fixed:max($0.005/股, $1/單)、上限 1% 成交值;敏感度 = 現行 7.5bps 比例制 |
| 期間 | 2018-07-01 → 2026-07(資料最後日),同 ADR-0004/0005 驗證基準 |
| 引擎保真 | 複製體(floor+flat)與上游 `SP500Backtester` **位元級等價**:同 700 筆交易、最終權益同為 $9,218,140.14、最大逐日差 9.3e-09;複製方法附來源 hash 漂移保護 |

## 2. 回測結果:整數稅 vs 資金規模

![整數稅四面板](output/frac_sizing/tax_vs_capital.png)

| 起始資金 | floor CAGR | 稅(frac−floor) | 稅(frac−round) | floor 部署率(p10) | floor L1 誤差 | 漏單週 |
|---|---|---|---|---|---|---|
| $2,000 | 27.84% | **+2.85pp** | +0.64pp | 96.1%(91.4%) | 9.2pp | 2.2% |
| $4,000 | 30.60% | **+1.53pp** | +0.53pp | 97.8%(94.6%) | 6.7pp | 0 |
| $8,000 | 32.46% | +0.42pp | +0.50pp | 98.6%(95.9%) | 5.4pp | 0 |
| **$8,239(Taylor)** | 32.29% | **+0.61pp** | −0.11pp | 98.6%(96.0%) | 5.2pp | 0 |
| **$11,349(Rosa)** | 32.98% | +0.10pp | +0.25pp | 98.8%(96.4%) | 5.1pp | 0 |
| $16,000 | 32.80% | +0.42pp | +0.05pp | 98.9%(96.3%) | 4.9pp | 0 |
| **$27,116(Yvonne)** | 33.15% | +0.20pp | +0.00pp | 98.9%(96.2%) | 4.7pp | 0 |
| $32k–$256k | 33.2–33.4% | +0.05~0.21pp | ±0.2pp | 99.0% | 4.4–4.7pp | 0 |
| $1,000,000 | 33.45% | +0.01pp | −0.00pp | 99.1% | 4.4pp | 0 |

**路徑噪音底 ≈ ±0.3pp**(證據:floor@$8,000 與 floor@$8,239 僅差 3% 資金卻差 0.17pp;$1M 時 frac−floor = +0.01pp)。$2k/$4k 的稅遠超噪音;$11k 以上單點差異不可與噪音區分,但**部署率/L1 誤差/漏單三個機械指標單調收斂**,方向確定。

**Taylor 錨點金額化(8 年)**:floor 終值 $76,849、round $80,217、frac $79,702——floor 相對 frac 少賺 **$2,853(−3.6% 財富)**;佣金三臂皆 ~$710,非差異來源。

**frac 臂的 L1 誤差底線是 4.4pp 而非 0**:這是 3% REBALANCE_THRESHOLD 刻意允許的漂移(設計內建的鬆弛),整數稅是疊在這條底線**之上**的額外失真。

## 3. Live 實證:今天的病比回測平均重(2026-04-02 → 07-03)

![Live 部署率](output/frac_sizing/static_sp500_deployment.png)

| 帳戶 | SP500 部署率 mean(min) | 現金拖累/sleeve | 個股最差 sizing 誤差 |
|---|---|---|---|
| Taylor | **89.1%(82.1%)** | 10.9% | MU mean −24%(worst −45%)、STX −19%(−43%) |
| Rosa | 91.5%(84.2%) | 8.5% | 同 cohort,倍數較輕 |
| Yvonne | 97.1%(91.9%) | 2.9% | 輕微 |

**回測平均與 live 現況的橋接**:回測 $8,239 臂的部署率平均 98.6%,但那是「起始 $8.2k、8 年複利滾到 $77k」的全程平均——後段資金大、病自然輕。Live 的 Taylor **現在就停在 $8.2k**,且 2026 cohort 平均股價 ~$750(粒度比 = 一股價/單檔理論部位 ≈ 50–76%)。以 live 實測缺口做一階估算:未部署 10.9% × sleeve CAGR ~33% ≈ 3.6pp/年,扣除 1.4x 槓桿下少借融資的利息節省(~0.5–1pp)→ **當前稅率 ~2.5–3pp/年**,與回測 $2k 臂(2.85pp)吻合。

**SR**:三帳戶部署率 97.9–99.4%(ETF 價低、粒度細)→ 病輕,不需碎股。
**SkewLS**:round() 半股誤差上界中位數 0.05–0.34% NAV(Taylor 最粗的腳 ±50%/腳,但金額小)→ 組合層級無關痛癢。
**⇒ 碎股化的主戰場只有 SP500 sleeve。**

## 4. 佣金模型敏感度

| 模型 | floor@8239 | frac@8239 | 差距 |
|---|---|---|---|
| IBKR Fixed(主) | 32.29% | 32.89% | +0.61pp |
| 7.5bps 比例制(現行回測) | 31.64% | 32.10% | +0.46pp |

方向與量級一致 → 臂間結論對佣金模型穩健。注意:7.5bps 設計上**含點差**,IBKR Fixed 臂只算佣金未含點差,故兩套絕對值都不宜直接對照既有 tearsheet;臂內對照(同模型)才是本研究的比較基礎。

## 5. IBKR 可行性(證據等級標注)

| 事項 | 狀態 | 證據等級 |
|---|---|---|
| 帳戶權限「全球(小數股交易)」 | Rosa 已開(其餘待確認) | **實測**(CP 對照實驗 2026-07-05) |
| 碎股 × MOO(開盤競價) | **被拒** | **實測**:同帳戶同 $100 NVDA,市價+TIF開盤價被拒、非 MOO 過閘;錯誤文案誤導性地怪帳戶權限 |
| 碎股 × 一般市價/限價 | 可下(cash quantity 確認框) | 實測(未成交級驗證) |
| 碎股 × MOC/LOC | 下拉可選、送單未測 | UI 觀察 |
| 碎股 × API(ib_insync/TWS API) | **官方 KB:「supported via FIX/CTCI but not via API at this time」;第三方 2026 指南稱可行** | 文件矛盾 → **$2 實單是唯一仲裁,live 化第一道 gate** |
| 揭露書白名單 | 「only accept certain types...(market, limit, stop, stop limit)」 | 官方文件(表 4289,2024-03-20) |
| 持倉記錄粒度 | 0.0001 股;碎股不可轉倉、無投票權 | 官方文件 |
| 隱藏截斷點 | `rebalance_ib.py` `int(current_pos)`(~L589)會把碎股持倉截斷(Rosa BTC 65.1783→65) | 程式碼;碎股化前必修 |
| live 化單型設計候選 | 混合單:整數部分 MOO + 碎數尾巴(<1 股)開盤後 RTH MKT/LMT 補單;尾巴金額 ≤ 一股價,滑價上限極小 | 設計提案(待回測後決策,已記錄) |

## 6. 建議(依序)

1. **立即可做(免費)**:SP500 sizing 的 `int()` → `int(round())`([rebalance_ib.py:246](../automation/rebalance_ib.py))。回測顯示 round 拿走 frac 好處的大部分、不需權限、不動 MOO、不碰 API。**代價**:單一部位可超配(理論 0.51 股 → 進位 1 股 = 超配最多 +96%,在 MU 這種 $1,084 股上 = 一檔多壓 6.6% NAV);整體統計贏、單點波動變大,上線前建議用 dry-run 對照一週。此為 live 變更,依「先純研究」原則**留待你點頭**。
2. **短期**:跑 $2 的 ib_insync 碎股實測(0.3 股遠價 LMT → 看收單/錯誤碼 → 立即撤),仲裁 API 支援度。腳本隨時可寫。
3. **中期(若 API 通)**:SP500 sleeve 碎股化 + 混合單改造;SR/SkewLS 維持整數(病輕,不值得動 MOO 相容性)。同時修 `int(current_pos)` 截斷點。此變更符合 ADR 三條件(難逆轉、未來讀者會問為什麼、真取捨),屆時開 ADR。
4. **監控**:daily/weekly report 加一行「SP500 sleeve 部署率」,讓整數稅持續可見(本研究的 log parser 可直接復用)。
5. **資金面**:若 Taylor 會再入金,$16k sleeve(NAV ~$32k @ alloc 50%)以上整數稅自然消失——入金本身就是治療。

## 附錄:重現

```
python -m robustness.frac_sizing_study --phase check    # 引擎保真(必須 PASS)
python -m robustness.frac_sizing_study --phase sweep    # 40 組掃描(可中斷續跑)
python -m robustness.frac_sizing_study --phase charts
python -m robustness.frac_static_analysis               # live log 實證
```
