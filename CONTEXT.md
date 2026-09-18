# QuantCore — Domain Context

自動化量化交易系統，把多個 [[Strategy]] 透過 [[Allocation]] 分派到各個 IB [[Account]] 執行。

## Language

**Account**:
對應一個 IB 帳戶（U-prefix），自有 NAV / 持倉 / Allocation 設定 / Telegram 通道，由 `accounts.json` 的 `name` 標識（Taylor / Rosa / Yvonne）。
_Avoid_: User, Client, Sub-account

**Strategy**:
一個自成體系的「訊號 → normalized 目標權重（內部 gross ≤ 1.0）」產生器，不知道帳戶存在，也不知道其他 Strategy 存在。滿倉 gross = 1.0，但**持現金是合法輸出**：[[SR]] 的 WEIGHT_STEP 取整殘差（gross 0.95，歷史 13% 天數，回測同源）與 z-threshold 全空手、[[SP500]] 的等待期 / dip 階梯部分配置；[[Skew LS]] 的 gross 依 regime allocation 另行定義。所有 Strategy 的 target 在帳戶層級加總（pure-function combine），IB 自動 net 多空。
目前定義：[[SR]]、[[SP500]]、[[Skew LS]]。
_Avoid_: Model, Signal generator, Engine

**Constituents**:
某個 [[Strategy]] 在指定日期可交易的 ticker 集合（時間函數）。[[SP500]] 的 constituents 隨 S&P index rebalance 變動；[[SR]] / [[Skew LS]] 的 constituents 固定為各自的 sector ETF 清單。Strategy 的訊號產生器只能從當日 constituents 裡選股。
代碼寫法：constituents 用 S&P 寫法（類股 `BRK.B`），行情檔用 provider 寫法（`BRK-B.csv`）；一般情況只做格式轉換（`strategies/sp500_momentum/utils.provider_symbol`），改名、合併、代碼重用不自動推論。#56 的 `data/historical_aliases.json` 是唯一例外：其中明列的 8 組歷史身份，只有在 inclusive session 日期界限內，才由 reader 與 coverage audit 共用 resolver 對應到 successor provider 檔；界限外仍不對應，跨股別（例如 DISCK）也不對應。下單用的 broker 代碼是另一套寫法，不在此資料契約內：IB 類股由 `automation/symbol_contract.py` 將 `BRK.B`／`BF.B` 轉成 `BRK B`／`BF B`，IB 回傳後再正規化回 constituents symbol；其他代碼維持原值。
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

**Combine**:
把多個 [[Strategy]] 的 target 在單一 [[Account]] 匯總成 per-ticker 目標股數的運算：每個 Strategy 先各自以 NAV × [[Allocation]] × weight ÷ price 取整成股數，再對同 ticker 加總（live 實作 = `automation/rebalance_plan.py::combine_targets`，`rebalance_ib.combine_targets` 為 re-export 舊路徑）。per-strategy 取整是 load-bearing，不可先合權重再取整（[ADR-0008](docs/adr/0008-live-combine-no-weight-level-extraction-rejected.md)）。與 backtest 的 return-level combine（`backtest/combo.py`）是不同層的運算，刻意不共用。
_Avoid_: Merge, Netting（sector-level netting 是被否決的另一提案，見 [ADR-0006](docs/adr/0006-sector-level-netting-rejected.md)）

**Rebalance Plan (調倉計畫)**:
單一 [[Account]] 一次調倉的完整決策：持倉分類（[[Universe]] 語意，含 SkewLS inverse 排除）→ mode 權重選擇（非 sp500 mode 的 hold 防護、SkewLS threshold gate）→ [[Combine]] → 差額 + 3% NAV 門檻（清倉/建倉/翻向例外）→ 訂單清單（PlannedOrder）。唯一實作 `automation/rebalance_plan.py`；scheduled rebalance（`rebalance_ib`，Task Scheduler 21:00）與 panel Sync Pos（`sync_position`）皆為 caller。純 plain-data 決策 — IB 連線、訊號計算、state 檔讀寫、LMT 價、下單順序等 execution/I-O 關切全留在 caller。歷史上四件事故（CIEN stale-universe、週二 whipsaw ADR-0004、MU floor 低配、inverse 凍結）皆發生於此層而彼時零測試 — 現以 `automation/tests/test_rebalance_plan.py` regression 考題釘住。
_Avoid_: Order list（只是最終輸出）、Rebalance（口語泛稱整個流程；指決策層時用本詞）

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
[[SP500]] 每個交易日盤前跑一次的檢查。原含停損 / 跳空出場 / 熊市 dip-buy，但停損與跳空已移除（[ADR-0004](docs/adr/0004-remove-sp500-fixed-stop-and-gap-exit.md)：回測零貢獻 + 製造 whipsaw），**現在只剩熊市 dip-buy 觸發**。個股出場改由週三 [[Rotation]] 負責。觸發即在 [[執行盤 (Execution Session)]] 送 LMT（outsideRTH）。
_Avoid_: Daily check, Stop loss job

**Rotation**:
[[SP500]] 每週三的持股輪動決策：持股「排名跌出前 20（SELL_RANK_THRESHOLD）」或「跌破 EMA50（EXIT_EMA）」者賣出，缺額依動能排名補足（過熱 adj_slope ≥ MAX_ADJ_SLOPE 與 [[噴出 (Blowoff)]] kurt60 > 門檻皆不佔名額，[ADR-0011](docs/adr/0011-sp500-blowoff-entry-block.md)），以 ATR 反比配權。rank 優先於 EMA — 同輪賣出又買回 = 續抱是刻意行為（[ADR-0005](docs/adr/0005-rank-priority-no-entry-ema-filter.md)）；個股出場只由 Rotation 負責（[ADR-0004](docs/adr/0004-remove-sp500-fixed-stop-and-gap-exit.md)）。唯一實作 `strategies/sp500_momentum/rotation.py`；live（signal.py）、backtest（backtester.py 與 frac-sizing 研究）皆為 caller，execution 關切（成交價、資金、取整、kept 的執行回饋）留在 caller。
_Avoid_: Rebalance（帳戶層 [[Combine]]+下單那一步）、選股（上游 ranking 是 SelectionEngine 的事）

**噴出 (Blowoff)**:
個股價格結構脫離「穩定上漲」範圍的狀態 — live 判定 = 60 日調整後日報酬 kurtosis > P90 錨定門檻（`rotation.is_blowoff`，唯一實作；研究面另含乖離/skewness，見 `robustness/takeprofit_common.py`）。與 [[Rotation]] 既有的「過熱」（adj_slope ≥ MAX_ADJ_SLOPE，擋「漲太快」）是**不同概念、不同因子**（噴出擋「漲相不穩」——少數極端日撐起的走勢），不互相重載。出場用途已否決（[ADR-0010](docs/adr/0010-sp500-take-profit-exit-rejected.md)）；**進場封鎖**（噴出不佔 [[Rotation]] 補位名額，只擋買、不砍倉）2026-07-11 上線（[ADR-0011](docs/adr/0011-sp500-blowoff-entry-block.md)）。
_Avoid_: 過熱（保留給 adj_slope 進場過濾）、乖離過大（研究面因子之一，非 live 判定）

**停利 (Take-Profit)**:
[[SP500]] 被否決的第三個 [[Rotation]] 賣出理由提案（獲利前提 + [[噴出 (Blowoff)]] 觸發）— 拆解回測證明出場貢獻 ≤ 0，見 [ADR-0010](docs/adr/0010-sp500-take-profit-exit-rejected.md)。個股出場維持「只由週三 Rotation 負責」（ADR-0004）。此詞條保留供未來提案比對，免重盤。
_Avoid_: 停損（方向相反且已被 ADR-0004 移除）、過熱出場、結構出場

**理論部位 (Target Notional)**:
單一 [[Account]] 對單一標的「取整前」的目標金額 = NAV × [[Allocation]] × Strategy weight。與 rebalance log 裡的 `target=Xsh`（取整**後**股數）是不同的東西——混用兩者是「1 股超出理論部位」這類誤判的根源。
_Avoid_: 目標部位（與 log 的 `target=Xsh` 混淆）、應有部位

**整數執行稅 (Integer Execution Tax)**:
[[理論部位 (Target Notional)]] 與整數股執行結果之間的系統性缺口。floor 取整（SR）→ 恆低配 + 現金拖累；round 取整（[[SP500]] 自 2026-07-05、[[Skew LS]]）→ 對稱 ±半股誤差、可超配。高價股 × 小預算時最重，隨權益複利上升而遞減。量化見 `robustness/frac_sizing_report.md`。
_Avoid_: Rounding error（不帶方向性，掩蓋 floor 的系統性偏低）

**值班 (Watch)**:
Panel 的三個定時監控統稱：Rosa poll（台北 21:00–05:00 市場窗每 30 分輪詢）、TY check（週二・三 20:05 開盤前驗證手動登入）、Morning check（NYSE 交易日 07:00 健檢 + constituents staleness 提醒）。排程＋派工唯一實作 = `automation/monitor_engine.py`（MonitorEngine + 三張 spec，ports 注入 I/O）；「該不該警報」的純決策與 `next_*_time` 排程計算在 rosa_poll / ty_check / morning_check 各自模組；`control_panel` 只是 Tk adapter（after 計時、worker 結果 marshal 回主線程、Telegram 出口）。防洪紀律：Gateway 斷線**事件不直發 TG**，Rosa 警報一律經 30 分輪詢 gate — 盤外 flapping 不洗版；事件路徑只有斷線 10 分鐘後的一次性 recovery（schedule_once）。
_Avoid_: Monitoring（與 GatewayMonitor 心跳連線層混淆）、Alarm job

**當日排程總結 (Daily Run Summary)**:
平日 21:31 成交回報最後固定的一行：當天 04:30 資料更新、04:40 每日報告、16:00 [[日度風控 (Daily Risk Check)]]、21:00 調倉、21:31 成交回報有沒有跑完、有沒有出錯；沒單沒事的日子 Telegram 只有這一行。依據 = 各排程入口（`--account all`）寫的執行紀錄 `automation/logs/run_status/<job>_<YYYYMMDD>.json`，實作 `automation/run_status.py`：沒有紀錄 = 沒執行；有開始沒結束 = 沒跑完；開始時間早於排程 = 手動跑的，不算。平日 21:35 沒收到 = 電腦、排程或 Telegram 本身出問題（#38）。
_Avoid_: Heartbeat（只代表程式活著，這裡還列各排程結果）、每日報告（= 04:40 帳戶快照）

## Relationships

- 一個 **Account** 持有多個 **Strategy** 的部位
- 一個 **Account** × 一個 **Strategy** 對應一個 **Allocation**（總共 3 × 3 = 9 個 alloc 值）
- **Leverage** = Σ **Allocation**（per Account）
- 每個 **Strategy** 都有自己的 **Constituents**（時間函數）與 **Universe**（歷史聯集）
- **SP500** 的 **Constituents** 隨 S&P index rebalance 變動；**SR** / **Skew LS** 的 **Constituents** 固定
- **Skew LS** 的 short signal → 多數透過 **Inverse Proxy** 轉長單；XLC 例外走 **Direct Short**
- 多個 **Strategy** 在同個 **Account** 的 target 由 **Combine** 匯總成 per-ticker 目標股數（per-strategy 取整後加總；IB net 多空）
- **Skew LS** 的 threshold rebal 需要 per-account state file（`skewls_state_{account}.json`）紀錄上次該策略自己的 target — 與 IB 真實部位無關（避免被其他 Strategy 的部位污染）
- **日度風控** 的 **前收 (Reference Close)** = 最近已收盤 session；**執行盤 (Execution Session)** = 即將到來、尚未收盤的下一盤。回測中兩者是相鄰兩個歷史日；live 中 Execution Session 尚未存在於資料，故 Reference Close 必須是資料的最後一筆已收列、而非倒數第二筆
- **停利**（研究中）觸發 = 獲利前提 AND **噴出**；**噴出**同時使該股不佔 **Rotation** 補位名額（與既有過熱 adj_slope 過濾並列的第二道進場封鎖）

## Example dialogue

> **Dev:** 「Rosa 的 `alloc_skewls = 0.3`，那 IB 端會看到大約 30% NAV 的反向 ETF 持倉嗎？」
> **Trader:** 「不，30% 是『對 sector skewness 因子的曝險』，是 underlying gross。經過 Inverse Proxy 把 2x 反向 ETF 拿來代理空頭後，IB 實際 margin 占用大約 27%。剩下 3% NAV 是 free lunch，不用付資金成本。」
> **Dev:** 「那 Leverage 1.5x 顯示的是因子曝險還是 margin 占用？」
> **Trader:** 「因子曝險。IB margin 占用會稍低，這是設計上的選擇，見 ADR-0001。」

## Flagged ambiguities

- 「leverage」曾被當成「可獨立設定的欄位」考慮過（grilling Q1）— 解決：永遠是 derived，調 leverage 必須透過調 alloc
- 「alloc_skewls = 0.3」是 factor exposure 還是 capital usage（grilling Q3）— 解決為 factor exposure，見 [ADR-0001](docs/adr/0001-alloc-skew-factor-exposure.md)
- 「SP500 [[Constituents]] 怎麼保持最新」原本無自動化機制，靜態 Excel `_2015_2026.xlsx`（grilling 2026-06-01）— 解決為 on-demand Wikipedia fetch + dense reconstruction，見 [ADR-0002](docs/adr/0002-sp500-constituents-on-demand-refresh.md)
- 「同 sector SR + SkewLS 是否要 net？例如 SR LONG XLK 同時 SkewLS LONG REW」（2026-06-01）— 7 年 backtest 顯示 netted 全 alloc 全年敗（-28~-52 bps/yr，leverage drift alpha 大於 friction 省），rejected，維持 pure-function combine，見 [ADR-0006](docs/adr/0006-sector-level-netting-rejected.md)
- 「SkewLS 部位 < 3% 或 5% NAV 是否該 skip 不建？省 \$1 min commission」（2026-06-01）— 7 年 backtest sweep 顯示 Yvonne 實況 alloc 下 threshold ≥ 1% 全部負分（3% Sharpe -0.060、5% Sharpe -0.099），friction 省 13 bps/yr 被 alpha loss 60 bps/yr 完全淹沒（小部位是 diversification 一環不是 noise），rejected，見 [ADR-0007](docs/adr/0007-min-position-filter-rejected.md)
- 「live [[Combine]] 是否該抽 weight-level pure function 與 backtest 共用」（架構 review 2026-06-05）— rejected，per-strategy 整數 rounding 是 load-bearing（`int(a)+int(b) ≠ int(a+b)`，SR/SkewLS sector ETF 常重疊），且 `combine_targets` 已測透、backtest combine 在 return level 本就不同運算，見 [ADR-0008](docs/adr/0008-live-combine-no-weight-level-extraction-rejected.md)
- [[日度風控 (Daily Risk Check)]] 把 [[執行盤 (Execution Session)]] 誤當成「資料最後一列」、[[前收 (Reference Close)]] 誤抓成倒數第二列，導致週五破停損線、週一卻不出場（grilling 2026-06-07）— 解決為 Reference Close = 最近已收盤 session（資料最後一列），含 staleness halt-guard 與報告同源標價，見 [ADR-0003](docs/adr/0003-live-daily-risk-timing-and-marking.md)
- 接續上點：8 年回測證實 20% 停損零貢獻、50% 跳空從未觸發，且 live 週二 `--mode all` 會用舊訊號把停損掉的股票買回（whipsaw，CIEN 2026-06）— 解決為**整個移除** SP500 固定停損與跳空出場，個股出場只由週三 rotation 負責，見 [ADR-0004](docs/adr/0004-remove-sp500-fixed-stop-and-gap-exit.md)
- 「EMA50 出場同輪被高排名再進場抵消（SELL 又 BUY 回，CIEN 2026-06-10）是 bug 還是設計？」— WF（N=8+N=7）否決進場 EMA 過濾後解決為**刻意行為**：rank 優先於 EMA，`ENTRY_EMA_FILTER=False`，見 [ADR-0005](docs/adr/0005-rank-priority-no-entry-ema-filter.md)
- 「SR gross sum 0.95 是 bug 還是設計？」`test_get_sr_weights_sum_approximately_one` 斷言 ≈1.0 常態性失敗（2026-07-05）— 診斷：z-filter 後 raw 權重 normalize 到恰好 1.0，四捨五入至 WEIGHT_STEP（5%）後**只有 >100% 一側被強制降回**，<100% 殘差刻意留現金（例 2026-06-29：raw 0.506/0.274/0.220 → 0.50/0.25/0.20）；回測 `compute_mom_weights` 同源，11.5 年中 13.2% 天數 gross=0.95、7% 空手。解決：live 權重**不動**（改正規化會偏離已驗證回測），測試斷言改為設計界限 gross ∈ [1 − n×step/2, 1.0]，[[Strategy]] 詞條的「gross sum = 1.0」修正為「gross ≤ 1.0」；同類問題 `test_get_sr_weights_returns_non_empty`（斷言至少 1 檔持倉，熊市空手 regime 會常態性失敗）同日修正 — 空手時改驗證 z-threshold 前置條件（無 ETF 同時 z > thresh 且 score > 0），更名 `test_get_sr_weights_non_empty_or_verified_all_cash`，以 2022 熊市 6 個空手週模擬驗證通過且對非空日有鑑別力
- 「Taylor 1 股 MU 超出理論部位」直覺（grilling 2026-07-05）— 7/1 實際數據證偽：floor 語意下 MU 為**低配 24%**、SP500 sleeve 僅部署 82%（現金拖累 ≈9% NAV）；超配只發生在 round() 的 [[Skew LS]] 小腳。命名釐清後拆成 [[理論部位 (Target Notional)]] 與 [[整數執行稅 (Integer Execution Tax)]] 兩詞。三臂 × 資金階梯研究**已完成**（`robustness/frac_sizing_report.md`）：[[整數執行稅 (Integer Execution Tax)]] 集中在小 sleeve × 高價股 — 回測 $2k 臂 −2.85pp CAGR、$11k 以上入 ±0.3pp 噪音底，但 live 實測 Taylor 當前部署率僅 89%（min 82%）≈ 每年 2.5–3pp；`int(round())` 一行改動可免費收回大部分；whipsaw 假說被三臂數據否決（3% threshold 已抑制）
- 「refresh_constituents 可以自動化嗎」（2026-07-05）— ADR-0002 拒絕的是 **silent** 自動污染，而現行實作全程有 gates+告警不 silent；on-demand 則已失效兩次（2026-01 CIEN、2026-06 凍 34 天）。解決為每週三 04:50 gated 自動寫入：全綠才寫+diff 報告、soft warn（G3/G5）不寫入只告警、7 天 stale 提醒轉任自動化 watchdog，見 [ADR-0009](docs/adr/0009-constituents-gated-auto-refresh.md)
- 「停利」提案時原指純結構條件出場（乖離/skew/kurt 超標即賣，不參照獲利）（grilling 2026-07-10）— 純結構規則自相矛盾：高乖離進場的股票（STX 2026-06-03 @940）當週就會被踢出、下輪 rank 優先又買回，重演 ADR-0004 移除停損時的 whipsaw。解決：[[停利 (Take-Profit)]] = 獲利前提（回合首筆進場價 +X%）AND [[噴出 (Blowoff)]]，只在週三 Rotation 評估，噴出條件對稱封鎖進場
- skew 方向衝突（grilling 2026-07-10）：[[Skew LS]] 在 **sector 層級做多高 skew**（skew premium），停利研究在**個股層級把高 skew 當出場訊號**（lottery/blowoff）— 同一個詞、同一 codebase、相反方向的賭注。解決：兩者是不同層級的不同異常，皆屬合法用法；個股層級高 skew 出場的有效性由回測+WF 裁決，不由類比推定
- 「過熱」一詞不重載（grilling 2026-07-10）：既有過熱專指 adj_slope ≥ MAX_ADJ_SLOPE 的進場過濾；新的結構條件另名 [[噴出 (Blowoff)]]，兩者因子不同（迴歸斜率×r² vs 乖離/skew/kurt）、作用端不同
- 「乖離過大該停利」直覺（研究 2026-07-11 結案）— 數據證偽且方向相反：乖離停利全 grid 慘敗（最緊門檻 CAGR 12.5% vs 32.1%），高乖離贏家是動能右尾的來源不是風險；skew/kurt 停利出場拆解後貢獻 ≤ 0（兩筆實際出場 META/PLTR 皆為壞出場）。唯一存活訊號是「噴出**進場**封鎖」（不買，但抱著的不動）且過 WF，見 [ADR-0010](docs/adr/0010-sp500-take-profit-exit-rejected.md) 與 `robustness/takeprofit_report.md`
