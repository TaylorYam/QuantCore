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

## Relationships

- 一個 **Account** 持有多個 **Strategy** 的部位
- 一個 **Account** × 一個 **Strategy** 對應一個 **Allocation**（總共 3 × 3 = 9 個 alloc 值）
- **Leverage** = Σ **Allocation**（per Account）
- 每個 **Strategy** 都有自己的 **Constituents**（時間函數）與 **Universe**（歷史聯集）
- **SP500** 的 **Constituents** 隨 S&P index rebalance 變動；**SR** / **Skew LS** 的 **Constituents** 固定
- **Skew LS** 的 short signal → 多數透過 **Inverse Proxy** 轉長單；XLC 例外走 **Direct Short**
- 多個 **Strategy** 在同個 **Account** 對同檔 ticker 的 target 直接加總（IB net 多空）
- **Skew LS** 的 threshold rebal 需要 per-account state file（`skewls_state_{account}.json`）紀錄上次該策略自己的 target — 與 IB 真實部位無關（避免被其他 Strategy 的部位污染）

## Example dialogue

> **Dev:** 「Rosa 的 `alloc_skewls = 0.3`，那 IB 端會看到大約 30% NAV 的反向 ETF 持倉嗎？」
> **Trader:** 「不，30% 是『對 sector skewness 因子的曝險』，是 underlying gross。經過 Inverse Proxy 把 2x 反向 ETF 拿來代理空頭後，IB 實際 margin 占用大約 27%。剩下 3% NAV 是 free lunch，不用付資金成本。」
> **Dev:** 「那 Leverage 1.5x 顯示的是因子曝險還是 margin 占用？」
> **Trader:** 「因子曝險。IB margin 占用會稍低，這是設計上的選擇，見 ADR-0001。」

## Flagged ambiguities

- 「leverage」曾被當成「可獨立設定的欄位」考慮過（grilling Q1）— 解決：永遠是 derived，調 leverage 必須透過調 alloc
- 「alloc_skewls = 0.3」是 factor exposure 還是 capital usage（grilling Q3）— 解決為 factor exposure，見 [ADR-0001](docs/adr/0001-alloc-skew-factor-exposure.md)
- 「SP500 [[Constituents]] 怎麼保持最新」原本無自動化機制，靜態 Excel `_2015_2026.xlsx`（grilling 2026-06-01）— 解決為 on-demand Wikipedia fetch + dense reconstruction，見 [ADR-0002](docs/adr/0002-sp500-constituents-on-demand-refresh.md)
