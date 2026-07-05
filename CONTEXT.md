# QuantCore — Domain Context

自動化量化交易系統，把多個 [[Strategy]] 透過 [[Allocation]] 分派到各個 IB [[Account]] 執行。

## Language

**Account**:
對應一個 IB 帳戶（U-prefix），自有 NAV / 持倉 / Allocation 設定 / Telegram 通道，由 `accounts.json` 的 `name` 標識（Taylor / Rosa / Yvonne）。
_Avoid_: User, Client, Sub-account

**Strategy**:
一個自成體系的「訊號 → normalized 目標權重（內部 gross sum = 1.0）」產生器，不知道帳戶存在，也不知道其他 Strategy 存在。所有 Strategy 的 target 在帳戶層級加總（pure-function combine），IB 自動 net 多空。
目前定義：[[SR]]、[[SP500]]、[[Skew LS]]。
_Avoid_: Model, Signal generator, Engine

**Constituents**:
某個 [[Strategy]] 在指定日期可交易的 ticker 集合（時間函數）。[[SP500]] 的 constituents 隨 S&P index rebalance 變動；[[SR]] / [[Skew LS]] 的 constituents 固定為各自的 sector ETF 清單。Strategy 的訊號產生器只能從當日 constituents 裡選股。
_Avoid_: Tradeable list, Selection pool, Ticker pool

**Universe**:
某個 [[Strategy]] 歷史上所有 [[Constituents]] 的聯集 — 包含已被移除的 ticker。用於分類 IB 既有持倉：例如某檔股票當年由 [[SP500]] 買進、現在已從 index 移除但仍持有，需歸入「SP500 holdings」分類而非誤判為「Other」。
_Avoid_: Holdings universe, Investment universe

**SR**:
Sector Rotation — 11 SPDR 板塊 ETF 多單，多週期動量分數選股。Long-only，週二調倉。

**SP500**:
SP500 Momentum — S&P 500 個股動量，4 檔。Long-only，含熊市 dip-buy 機制，週三調倉。

**Skew LS**:
Sector Skew Long-Short — 偏態因子多空，多單持原 sector ETF、空單透過 [[Inverse Proxy]] + [[Direct Short]] 實現，threshold-based 換倉。

**Allocation**:
單一 [[Account]] 給單一 [[Strategy]] 的 scalar multiplier。語意為「對該策略 underlying factor 曝險佔 NAV 的百分比」。三個 alloc 完全獨立、不需相加為 1。`accounts.json` 欄位：`alloc_sr` / `alloc_sp500` / `alloc_skewls`。詳細語意見 [ADR-0001](docs/adr/0001-alloc-skew-factor-exposure.md)。
_Avoid_: Weight, Capital share, Position size

**Leverage**:
帳戶的總曝險倍數，derived value，永遠 = `alloc_sr + alloc_sp500 + alloc_skewls`。UI 顯示用，**非可獨立設定的欄位**。注意：因為 [[Inverse Proxy]] 的 2x 反向 ETF 用一半資金即達同樣風險曝險，IB 帳戶實際 margin 占用會略低於此值。
_Avoid_: Gearing, Total exposure (作 derived 顯示時可以；作獨立欄位時不行)

**Inverse Proxy**:
[[Skew LS]] 把空頭部位轉成反向 ETF 多單的轉換（9 檔 ProShares 2x、SEF/REK 1x）。Long position，IB 端走既有 BUY/SELL 路徑。
_Avoid_: Hedge, Synthetic short

**Direct Short**:
[[Skew LS]] 對 XLC 的處理方式（無對應反向 ETF），透過 IB margin account `SELL` 進場、`BUY_TO_COVER` 平倉。QuantCore 第一個 short side handling — 既有 SR / SP500 都是 long-only。
_Avoid_: Naked short

**執行盤 (Execution Session)**:
某筆訂單實際成交所對應的 NYSE session。[[SP500]] 的 [[日度風控 (Daily Risk Check)]] 在盤前送單，其 Execution Session 是「即將開盤、尚未收盤的下一盤」——因此**不存在於**收盤後才寫入的歷史資料中。
_Avoid_: Today, Trade day（"today" 在 live 與 backtest 語意不同，是 off-by-one 的根源）

**前收 (Reference Close)**:
「最近一個**已收盤**的 session」的收盤，= 資料最後一列 `index[-1]`。用於 [[日度風控 (Daily Risk Check)]] 的資料新鮮度 guard、dip-buy 的 regime 判斷，以及 daily_report 的部位標價（[ADR-0003](docs/adr/0003-live-daily-risk-timing-and-marking.md)）。在 live 盤前，Reference Close（已收）與 [[執行盤 (Execution Session)]]（未開）是**相鄰但不同**的兩盤。
_Avoid_: 前一日, Previous bar

**日度風控 (Daily Risk Check)**:
[[SP500]] 每個交易日盤前跑一次的檢查。原含停損 / 跳空出場 / 熊市 dip-buy，但停損與跳空已移除（[ADR-0004](docs/adr/0004-remove-sp500-fixed-stop-and-gap-exit.md)：回測零貢獻 + 製造 whipsaw），**現在只剩熊市 dip-buy 觸發**。個股出場改由週三 rotation（EMA50/排名）負責。觸發即在 [[執行盤 (Execution Session)]] 送 LMT（outsideRTH）。
_Avoid_: Daily check, Stop loss job

**理論部位 (Target Notional)**:
單一 [[Account]] 對單一標的「取整前」的目標金額 = NAV × [[Allocation]] × Strategy weight。與 rebalance log 裡的 `target=Xsh`（取整**後**股數）是不同的東西——混用兩者是「1 股超出理論部位」這類誤判的根源。
_Avoid_: 目標部位（與 log 的 `target=Xsh` 混淆）、應有部位

**整數執行稅 (Integer Execution Tax)**:
[[理論部位 (Target Notional)]] 與整數股執行結果之間的系統性缺口。floor 取整（SR）→ 恆低配 + 現金拖累；round 取整（[[SP500]] 自 2026-07-05、[[Skew LS]]）→ 對稱 ±半股誤差、可超配。高價股 × 小預算時最重，隨權益複利上升而遞減。量化見 `robustness/frac_sizing_report.md`。
_Avoid_: Rounding error（不帶方向性，掩蓋 floor 的系統性偏低）

## Relationships

- 一個 **Account** 持有多個 **Strategy** 的部位
- 一個 **Account** × 一個 **Strategy** 對應一個 **Allocation**（總共 3 × 3 = 9 個 alloc 值）
- **Leverage** = Σ **Allocation**（per Account）
- 每個 **Strategy** 都有自己的 **Constituents**（時間函數）與 **Universe**（歷史聯集）
- **SP500** 的 **Constituents** 隨 S&P index rebalance 變動；**SR** / **Skew LS** 的 **Constituents** 固定
- **Skew LS** 的 short signal → 多數透過 **Inverse Proxy** 轉長單；XLC 例外走 **Direct Short**
- 多個 **Strategy** 在同個 **Account** 對同檔 ticker 的 target 直接加總（IB net 多空）
- **Skew LS** 的 threshold rebal 需要 per-account state file（`skewls_state_{account}.json`）紀錄上次該策略自己的 target — 與 IB 真實部位無關（避免被其他 Strategy 的部位污染）
- **日度風控** 的 **前收 (Reference Close)** = 最近已收盤 session；**執行盤 (Execution Session)** = 即將到來、尚未收盤的下一盤。回測中兩者是相鄰兩個歷史日；live 中 Execution Session 尚未存在於資料，故 Reference Close 必須是資料的最後一筆已收列、而非倒數第二筆

## Example dialogue

> **Dev:** 「Rosa 的 `alloc_skewls = 0.3`，那 IB 端會看到大約 30% NAV 的反向 ETF 持倉嗎？」
> **Trader:** 「不，30% 是『對 sector skewness 因子的曝險』，是 underlying gross。經過 Inverse Proxy 把 2x 反向 ETF 拿來代理空頭後，IB 實際 margin 占用大約 27%。剩下 3% NAV 是 free lunch，不用付資金成本。」
> **Dev:** 「那 Leverage 1.5x 顯示的是因子曝險還是 margin 占用？」
> **Trader:** 「因子曝險。IB margin 占用會稍低，這是設計上的選擇，見 ADR-0001。」

## Flagged ambiguities

- 「leverage」曾被當成「可獨立設定的欄位」考慮過（grilling Q1）— 解決：永遠是 derived，調 leverage 必須透過調 alloc
- 「alloc_skewls = 0.3」是 factor exposure 還是 capital usage（grilling Q3）— 解決為 factor exposure，見 [ADR-0001](docs/adr/0001-alloc-skew-factor-exposure.md)
- 「SP500 [[Constituents]] 怎麼保持最新」原本無自動化機制，靜態 Excel `_2015_2026.xlsx`（grilling 2026-06-01）— 解決為 on-demand Wikipedia fetch + dense reconstruction，見 [ADR-0002](docs/adr/0002-sp500-constituents-on-demand-refresh.md)
- [[日度風控 (Daily Risk Check)]] 把 [[執行盤 (Execution Session)]] 誤當成「資料最後一列」、[[前收 (Reference Close)]] 誤抓成倒數第二列，導致週五破停損線、週一卻不出場（grilling 2026-06-07）— 解決為 Reference Close = 最近已收盤 session（資料最後一列），含 staleness halt-guard 與報告同源標價，見 [ADR-0003](docs/adr/0003-live-daily-risk-timing-and-marking.md)
- 接續上點：8 年回測證實 20% 停損零貢獻、50% 跳空從未觸發，且 live 週二 `--mode all` 會用舊訊號把停損掉的股票買回（whipsaw，CIEN 2026-06）— 解決為**整個移除** SP500 固定停損與跳空出場，個股出場只由週三 rotation 負責，見 [ADR-0004](docs/adr/0004-remove-sp500-fixed-stop-and-gap-exit.md)
- 「EMA50 出場同輪被高排名再進場抵消（SELL 又 BUY 回，CIEN 2026-06-10）是 bug 還是設計？」— WF（N=8+N=7）否決進場 EMA 過濾後解決為**刻意行為**：rank 優先於 EMA，`ENTRY_EMA_FILTER=False`，見 [ADR-0005](docs/adr/0005-rank-priority-no-entry-ema-filter.md)
- 「Taylor 1 股 MU 超出理論部位」直覺（grilling 2026-07-05）— 7/1 實際數據證偽：floor 語意下 MU 為**低配 24%**、SP500 sleeve 僅部署 82%（現金拖累 ≈9% NAV）；超配只發生在 round() 的 [[Skew LS]] 小腳。命名釐清後拆成 [[理論部位 (Target Notional)]] 與 [[整數執行稅 (Integer Execution Tax)]] 兩詞。三臂 × 資金階梯研究**已完成**（`robustness/frac_sizing_report.md`）：[[整數執行稅 (Integer Execution Tax)]] 集中在小 sleeve × 高價股 — 回測 $2k 臂 −2.85pp CAGR、$11k 以上入 ±0.3pp 噪音底，但 live 實測 Taylor 當前部署率僅 89%（min 82%）≈ 每年 2.5–3pp；`int(round())` 一行改動可免費收回大部分；whipsaw 假說被三臂數據否決（3% threshold 已抑制）
