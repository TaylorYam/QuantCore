# QuantCore — Taylor

Sector Rotation MOM + S&P 500 個股動量，透過 Interactive Brokers 自動化執行。

> Built by Taylor | Automated Quant Trading on IB Gateway

---

## 系統架構

```
QuantCore-master/
├── strategies/                 # 策略引擎
│   ├── sector_rotation/        #   板塊輪動動量 (SR)
│   ├── sp500_momentum/         #   S&P 500 個股動量 (SP500)
│   └── sector_skew_ls/         #   板塊偏態多空 (Skew LS)
├── backtest/                   # 回測框架（組合、績效、圖表、槓桿模型）
├── robustness/                 # Walk-Forward / Monte Carlo / 參數敏感度研究
├── data/                       # 市場資料（gitignored）+ 資料工具
│   ├── update_data.py          #   增量更新腳本
│   ├── refresh_constituents.py #   SP500 成分股自動更新（ADR-0009）
│   ├── checksum.py             #   資料完整性校驗
│   ├── migrate_data.py         #   可攜式資料根目錄遷移
│   ├── etfs/                   #   板塊 ETF 日線 CSV
│   ├── stocks/                 #   S&P 500 成分股日線 CSV（500+）
│   └── reference/              #   成分股歷史 / 交易日曆
├── automation/                 # 自動化交易 + 控制面板
│   ├── control_panel.py        #   Tkinter GUI 控制面板
│   ├── qt_panel/               #   新 PySide6 Panel（並行中，ADR-0012）
│   ├── rebalance_plan.py       #   調倉決策（純函式，不碰 IB）
│   ├── rebalance_ib.py         #   IB Gateway 自動調倉（caller）
│   ├── signals.py              #   策略訊號整合（SR / SP500 / Skew LS）
│   ├── account_ctx.py          #   帳戶上下文 dataclass
│   ├── monitor_engine.py       #   值班引擎（Rosa poll / TY check / Morning check）
│   ├── tray_controller.py      #   系統匣圖示（Gateway 連線狀態）
│   ├── tests/                  #   305+ 離線單元測試
│   ├── config.json             #   連線 / 配置中心（gitignored）
│   ├── skewls_state_example.json #  SkewLS state 範本
│   └── sp500_state_example.json  #  SP500 state 範本
├── research/                   # 探索性研究（不在 CI 收集範圍）
│   ├── kl_drift/               #   KL 散度因子漂移研究
│   └── momentum_acceleration/  #   動量加速度因子研究
├── shared/                     # 分享版工具（Dashboard / 回測 / 資料更新）
├── reports/                    # Walk-Forward 驗證報告（CSV）
├── docs/
│   ├── adr/                    #   架構決策紀錄（ADR-0001 ~ 0011）
│   ├── history/plans/          #   歷史開發計畫（PLAN.md / PLAN_R.md）
│   └── setup_new_computer.md   #   新電腦環境設置指南
├── CONTEXT.md                  # 領域語言定義（所有名詞以此為準）
├── AGENTS.md                   # AI / 人類協作規則
├── pyproject.toml              # 專案 metadata + 依賴宣告（PEP 621）
├── requirements.txt            # Python 依賴（lower-bound pins）
├── requirements.lock           # 精確版本鎖（可重現環境）
└── .github/workflows/ci.yml   # CI（PR 自動測試）
```

---

## 策略簡介

### 1. Sector Rotation MOM（板塊輪動動量）

以 11 支 SPDR 板塊 ETF 為標的池，下分兩個子策略。目前三帳戶皆以 Skew LS 為主力（alloc_skewls = 1.00），SR 停用（alloc_sr = 0）。

#### 1a. SR — Sector Rotation（板塊輪動，Long-Only）

| 項目 | 設定 |
|------|------|
| 標的池 | 11 支 SPDR 板塊 ETF（XLK、XLF、XLV、XLE 等） |
| 訊號 | 多週期加權動量（5/10/20/60 日，權重 0.4/0.3/0.2/0.1） |
| 進場條件 | Z-score > threshold |
| 持倉權重 | 圓整至 5%（WEIGHT_STEP），gross ≤ 1.0（殘差留現金） |
| 調倉頻率 | 每週二 |
| 目前狀態 | **停用**（alloc_sr = 0；WF 驗證 SR 為拖累，見 alloc WF 研究） |

#### 1b. Skew LS — Sector Skew Long-Short（板塊偏態多空）

| 項目 | 設定 |
|------|------|
| 標的池 | 11 板塊 ETF（多）+ 反向 ETF / Direct Short（空） |
| 訊號 | 偏態因子多空 |
| 空頭實現 | Inverse Proxy（2x 反向 ETF 代理空頭，[ADR-0001](docs/adr/0001-alloc-skew-factor-exposure.md)）；XLC 走 Direct Short |
| 換倉邏輯 | Threshold-based（state file 紀錄上次 target） |
| 目前狀態 | **主力**（alloc_skewls = 1.00） |

### 2. S&P 500 Momentum（個股動量 — Margin Stock）

| 項目 | 設定 |
|------|------|
| 標的池 | S&P 500 成分股（動態追蹤歷史成分變動，[ADR-0009](docs/adr/0009-constituents-gated-auto-refresh.md)） |
| 訊號 | 90 日動量排名，ATR 反比例加權 |
| 持股數 | 4 檔 |
| 賣出條件 | 排名跌出 Top 20 或跌破 EMA50（個股出場只由週三 Rotation 負責，[ADR-0004](docs/adr/0004-remove-sp500-fixed-stop-and-gap-exit.md)） |
| 進場過濾 | 過熱保護（adj_slope ≥ MAX_ADJ_SLOPE）+ 噴出進場封鎖（kurt60 > P90，[ADR-0011](docs/adr/0011-sp500-blowoff-entry-block.md)） |
| 調倉頻率 | 每週三 |
| 熊市機制 | SPY MA200 偵測，觸發時轉入 SPY dip-buy |

### 組合配置

Allocation 獨立設定（factor-exposure 語意），不需相加為 1：

```
Leverage = alloc_sr + alloc_sp500 + alloc_skewls
```

目前三帳戶配置（z-family；實際值以 `automation/accounts.json` 為準，2026-09-16 核對）：

| 帳戶 | alloc_sr | alloc_sp500 | alloc_skewls | Leverage | z-ladder 檔位 |
|------|----------|-------------|--------------|----------|---------------|
| Yvonne | 0 | 0.40 | 1.00 | 1.4x | z14 |
| Rosa | 0 | 0.40 | 1.00 | 1.4x | z14 |
| Taylor | 0 | 0.60 | 1.00 | 1.6x | z16 |

核心特徵：100% Skew LS + SP500 個股動量作為 margin stock，SR 停用。三帳戶都在 z-ladder 上（SkewLS 固定 1.00、SR 0，SP500 sleeve 決定槓桿檔位）；2026-07 的 WF + Monte Carlo 研究把上限檔位定在 Rosa z17、Taylor z18，實際採用較保守的檔位。詳見 [CONTEXT.md](CONTEXT.md) 的 Allocation / Leverage 定義與 [ADR-0001](docs/adr/0001-alloc-skew-factor-exposure.md)。

---

## 自動化流程

```
台灣時間          動作                         說明
──────────────────────────────────────────────────────────
04:30  每日       資料更新                      yfinance 抓取最新收盤價
04:40  每日       帳戶快照 → Excel              Margin Cushion < 25% 或失敗才發 Telegram
04:50  週三       Constituents 自動 refresh     gated 寫入 + diff 報告（ADR-0009）
07:00  交易日     Morning check                 健檢 + constituents staleness 提醒
16:00  交易日     SP500 Daily Check             熊市 dip-buy 觸發（LMT outsideRTH）
20:05  週二/三    TY check                      驗證 Gateway 手動登入
21:00  週二       Tue Rebalance (--mode all)     三策略合併：SR + SP500 hold + SkewLS check
21:00  週三       Wed Rebalance (--mode all)     三策略合併：SR hold + SP500 + SkewLS check
21:00  週一/四/五 SkewLS Check (--mode skewls)   獨立 threshold check（Tue/Wed 由上方涵蓋）
21:00–05:00      Rosa poll                      Gateway 連線狀態 30 分鐘輪詢
21:31  平日       MOO 成交回報 → Telegram        開盤後 1 分鐘檢查成交（含成交價）＋當日排程總結
```

- **MOO（Market-on-Open）**：保證以開盤價成交，須在美東 09:28 前送出
- 台灣 21:00 = 美東 09:00（夏令），留有 28 分鐘緩衝
- **Telegram**：只發與下單有關的訊息和錯誤（#26）。21:00 送單清單、21:31 成交回報、16:00 dip-buy 單；同一排程的三帳戶合成一則，沒單就不發。資料更新、TY check、constituents refresh 成功時不通知，失敗照發
- **當日排程總結**（#38）：平日 21:31 固定一行，列 04:30 資料更新、04:40 每日報告、16:00 日度風控、21:00 調倉、21:31 成交回報有沒有跑完、有沒有出錯；沒單的日子訊息只有這一行，有單時附在成交回報最後。**平日 21:35 還沒收到，就是電腦、排程或 Telegram 出問題**。依據是各排程寫的執行紀錄 `automation/logs/run_status/`

---

## 控制面板

Tkinter GUI 儀表板（`automation/QuantCore_Panel.vbs` 靜默啟動；同時只會開一個，重複啟動會叫出既有視窗）：

- **切換到新 Panel 後只看**：`config.json` 的 `qt_panel_mode` 為 `production` 時，Tk Panel 不值班、不能送單（手動下單／Sync Pos 只跳說明），視窗標題寫「只看」；改回 `parallel` 後重開即恢復（[docs/qt-panel-cutover.md](docs/qt-panel-cutover.md)，#154）
- **三帳戶並列**：Taylor / Rosa / Yvonne 各一欄，顯示 Net Asset Value、Unrealized P&L、Margin Cushion、Strategy Exposure、Account Exposure，持倉圓餅圖（小部位合併成「+N more」），以及依 ETF / 個股 / 手動分組的持倉表（UL、SH、Wt%、Cost、Price、P&L、P&L%）
- **Strategy Parameters**：每帳戶的 ETF / Stocks / SkewLS 配置滑桿，按 Save 確認後寫回 `accounts.json`
- **操作列**：`Sync Pos`（先選帳戶，視窗立刻出現並顯示計算進度，算完換成該帳戶理論持倉與 IB 實際持倉的差額預覽，確認後送限價單）、`Refresh IB`（手動刷新三帳戶）、`Log`（輸出紀錄彈窗）；右側顯示資料日期
- **Recent Trades**：每帳戶一個按鈕，彈窗顯示近 7 天的交易與成交價
- **自動刷新**：開啟時刷新一次，之後在美股交易時段固定時間刷新（美東 09:30、10:00–16:00 每小時；提前收盤日到 13:00）；標題列顯示各帳戶上次刷新時間
- **Gateway 監控**：Gateway Status 列顯示三帳戶連線狀態，`Check Now` 重新探測；Rosa poll、TY check、Morning check 三個定時監控只在異常時發 Telegram
- **系統匣**：關閉視窗只會縮到系統匣，監控照常運作；圖示顏色顯示 Rosa Gateway 狀態（綠 / 紅 / 灰），從系統匣選「離開」才會結束

### 新 Panel（PySide6）

Panel 正逐步換成 PySide6（[ADR-0012](docs/adr/0012-panel-pyside6-parallel-cutover.md)，spec #144）。切換前新舊並存：新 Panel **對實盤只看不下單、不值班**（Trading 只接 paper），IB 連線用自己的唯讀 clientId（`ROLE_PANEL_READONLY`），和 Tk Panel 同時開不互搶。切換（正式模式）、檢查清單與回退步驟見 [docs/qt-panel-cutover.md](docs/qt-panel-cutover.md)；下面描述的是並行模式。

- 安裝：`pip install -r requirements.txt`（含 `PySide6-Essentials`、`pyqtgraph`）
- 建捷徑：`python automation/create_shortcut.py --qt`（桌面「QuantCore Qt — Taylor」，走 `automation/QuantCore_QtPanel.vbs`）
- 目前內容：
  - Dashboard（已和 Tk 對等）：
    - 三個 Gateway 連線狀態（心跳斷了不會自己重連：按「重新檢查」＝ Tk 的 Check Now；另外比照 Morning check，每個 NYSE 交易日台北 07:00 自動重新檢查一次，只重連已斷的帳戶、不發通知）
    - 「一鍵啟動 Gateway」與「Gateway 帳密」：規則同 Tk 的「啟動 Gateway」（三個都要先關閉否則整批拒絕、依序登入、失敗不重試），帳密共用 Windows 認證管理員；兩邊都能按，但不要兩邊同時按（開始時才檢查有沒有 Gateway 在跑，兩個 Panel 之間沒有鎖）
    - 值班：Rosa poll／TY check／Morning check 的下次時間，以及 Tk Panel 有沒有開（沒開 = 目前沒有人值班）；新 Panel 自己不值班、不發 Telegram
    - 今日排程（當日排程總結的畫面版，讀 `logs/run_status/`；21:31 成交回報只看它今天的 log 有沒有寫過）、資料日期（`SPY.csv` 最後一天）、最後更新時間與「重新整理」、Output Log
  - Portfolio：上方三帳戶總覽（權益、未平倉損益、Account Exposure／Cushion、讀數來源），點選後下方顯示該帳戶的指標卡、配置圓環與持倉表（歸屬 SR／SkewLS／SP500／手動；策略外持倉與碰撞進策略 Universe 的手動持倉都標手動，唯讀讀 `manual_attribution` 紀錄；可依歸屬篩選、點欄位排序）。讀取排程同 Tk（開啟時一次、美股盤中固定時間），「重新讀取 IB」手動讀，Gateway 一鍵登入後也會自動讀一次；某帳戶讀取失敗只影響它自己，有上次讀數就照樣顯示並標「快取」。查詢用新 Panel 自己的唯讀 clientId（Taylor 69／Rosa 79／Yvonne 89），不和心跳（29／39／49）、Tk、排程撞號。指標卡、持倉列、圓環資料與 Tk 共用 `automation/portfolio_display.py`
    - 總覽卡片另有近 60 天權益小圖與報酬；單一帳戶頁有 pyqtgraph 權益曲線（滾輪縮放、拖曳平移時間軸，縱軸自動跟著區間；滑鼠移上去看日期、權益、距前高；1M／3M／YTD／1Y／全部 區間按鈕，預設 1Y）。資料同 Telegram 權益曲線（`equity_chart.load_equity_series`，`portfolio_tracker.xlsx` 的 Daily），Telegram 的圖仍用 matplotlib
    - 策略參數：各 Strategy 的 Allocation 與合計 Leverage（`accounts.json`，唯讀；新 Panel 沒有寫入 accounts／config 的路徑，調整照舊用 Tk Panel）；最近成交：`trade_log.xlsx` 近 7 天（顯示完整日期時間）。兩者的顯示計算與 Tk 共用 `automation/strategy_params_display.py`、`automation/trades_display.py`
    - 權益紀錄、策略設定、成交紀錄在背景讀：開啟時一次、按「重新讀取 IB」時再讀；某帳戶讀取失敗只影響它自己
    - 權益／業績切換（#166）：業績是排除出入金的累積報酬（同 IB 的 Performance），選區間時從區間起點 0% 算起；總覽卡片的「近 60 天」有出入金紀錄時也改算業績。出入金來自 IB Flex 報表（見下方設定），沒設定的帳戶只能看權益
- 業績圖的出入金設定（每個帳戶各做一次）：
  1. IB Client Portal → Performance & Reports → Flex Queries：新增一個 Activity Flex Query，勾 **Cash Transactions** 裡的 **Deposits & Withdrawals**，欄位選 **Select All**（至少要有 reportDate、amount、currency、fxRateToBase、transactionID），Period 選 **Last 365 Calendar Days**，Format 選 XML，記下 Query ID（純數字，不是 U 開頭的帳號）
  2. 同一頁的 Flex Web Service Configuration：開啟並產生 token（有效期限最長一年，到期要重新產生）
  3. 在本機 `automation/accounts.json` 該帳戶加上 `"flex_token"`、`"flex_query_id"`（範本見 `accounts.example.json`）。token 是機密，不要貼到任何地方
  - 新 Panel 開啟時與按「重新讀取 IB」時下載，同一天成功下載過就不再下載；結果合併存在 `automation/flex_cash_flows.json`（gitignored，只有日期與金額），所以超過一年的舊出入金也會保留。Flex 走 IB 網站，不經過 Gateway、不佔 clientId、不下單
  - Trading（#151）：手動單，流程同 Tk 的「手動下單」（選帳戶、代號建議、方向、股數／美元金額／權益 %、預覽含限價偏離警示與盤前盤後／隔夜標示、再次核對後跳確認視窗、委託狀態含部分成交／取消／修改／過期）；欄位解析、預覽與狀態文字和 Tk 共用 `automation/manual_order_form.py`。英數欄位關閉輸入法、全形自動轉半形（待注音實測）。輸入方式預設「帳戶權益 %」（Tk 仍預設整股股數）
    - **並行期間只接 paper**：送單連線一律經過送單閘門（`automation/qt_panel/broker_gate.py`），連上 Gateway 先讀它管理的帳號，不是全部 DU/DF 開頭就斷線拒絕、畫面說明原因，送單／撤單前再檢查一次。實盤下單照舊用 Tk Panel
    - paper 連線設定：在本機 `automation/config.json` 加上 `"qt_panel_paper": {"port": <paper Gateway 的 API port>, "account": "DU…"}`（可選 `host`，預設 127.0.0.1；`client_id`，預設 92）。沒設定、帳號不是 DU/DF、或 port 等於三個實盤 Gateway 之一時，Trading 只顯示原因、不能送單。帳戶選單照列三個帳戶，但全部送到這個 paper 帳號
    - paper 單另存 `automation/qt_panel_paper_orders.sqlite3`（gitignored），不混進 Tk 的 `manual_orders.sqlite3`；結案通知只寫 Output Log，不發 Telegram
  - Trading 的 Sync Pos（#152）：上方切換「手動單／Sync Pos」。選帳戶 → 計算（顯示進度）→ 預覽理論持倉、實際持倉、手動持倉暫停原因、交易清單 → 確認視窗才送 LMT；計算中按「放棄」或關掉 Panel，結果直接丟掉不送。預覽文字與 Tk 共用 `automation/sync_preview_rows.py`，計算與送單沿用 `sync_position`
    - **paper 不寫任何實盤紀錄**：理論持倉用所選帳戶的策略設定（Allocation）乘上 paper 帳戶的權益；連線走送單閘門（paper clientId = `client_id` + 1，預設 93）；SP500／SkewLS state 只讀不寫；手動持倉歸屬用 `automation/qt_panel_paper_attribution.sqlite3`（gitignored，手動單成交讀 paper 單紀錄）；不寫 `trade_log.xlsx`、不發 Telegram，結果與暫停通知只寫 Output Log；不向實盤 Gateway 借報價
    - paper 限制：三個帳戶名稱都送到同一個 paper 帳號，但手動歸屬按帳戶名稱記錄；在 paper 上測碰撞暫停時，手動單、Sync Pos、接管請選同一個帳戶名稱
  - Trading 的交給策略接管（#153）：上方切換的第三個「交給策略接管」，或在 Sync Pos 預覽的暫停列按「交給策略接管」直接帶入（同 Tk，該份 Sync 預覽作廢）。選帳戶與被暫停的標的 → 計算預覽（顯示進度）→ 手動紀錄／IB 現有股數、策略整數目標與差額 → 確認視窗才送這一檔的 LMT。不能接管（未結案手動單、IB 上還有掛單、沒有被暫停等）在狀態列說明原因；確認時資料已變動 → 換上新預覽並標出原因，要再確認一次。預覽與送單沿用 `strategy_takeover`，按鈕與確認文字和 Tk 共用 `automation/takeover_preview_text.py`
    - paper 同 Sync Pos：連線走送單閘門（paper clientId = `client_id` + 2，預設 94，與 Sync Pos 分開，比照 Tk 的接管 clientId）；歸屬與手動單紀錄用 paper 專用檔；不寫 state、不發 Telegram，接管結果只寫 Output Log
- 模式由 `config.json` 的 `qt_panel_mode` 決定，預設並行模式（`parallel`）。改成 `production`（#154）＝新 Panel 值班（Rosa poll／TY check／Morning check，同一套 `monitor_engine`）並送實盤單，改用 Tk 原有的 clientId role、紀錄與 Telegram；Tk Panel 讀同一個設定，自動不值班、不能送單、改用唯讀身分。兩個 Panel 都要重開才生效。步驟見 [docs/qt-panel-cutover.md](docs/qt-panel-cutover.md)

---

## 回測

```bash
# 基本回測（預設配置）
python -m backtest.run_all

# z-family 配置（SR=0, SP500=0.8, SkewLS=1.0）+ QuantStats 報告
python -m backtest.run_all --alloc 0 0.8 1.0 --quantstats

# 指定起始日
python -m backtest.run_all --start 2019-01-01 --quantstats
```

輸出位於 `output/`（gitignored）。

---

## 快速開始

### 環境需求

- Python 3.11+
- Windows（Task Scheduler 排程）
- Interactive Brokers Gateway

### 安裝

```bash
git clone https://github.com/TaylorYam/QuantCore.git
cd QuantCore
pip install -r requirements.txt        # lower-bound pins（允許 patch 升級）
# 或精確重現已驗證環境：
pip install -r requirements.lock       # 精確版本鎖
```

可選：
```bash
pip install pandas_market_calendars   # NYSE 假日精確排程
pip install quantstats                # 回測 HTML 報告
```

更新 lock file（在已驗證環境上執行）：
```bash
pip freeze > requirements.lock
```

### 設定

1. 複製範本：`automation/config.example.json` → `automation/config.json`
2. 複製範本：`automation/accounts.example.json` → `automation/accounts.json`
3. 填入 IB 帳號、port、Telegram token 等
4. 從 Google Drive 下載資料解壓縮到 `data/`（見 [setup guide](docs/setup_new_computer.md)）

### 啟動

1. **回測驗證**：`python -m backtest.run_all --alloc 0 0.8 1.0 --quantstats`
2. **更新資料**：`python automation/update_data.py`
3. **Dry Run 測試**：確保 `automation/config.json` 的 `dry_run: true`（`accounts.json` 可逐帳戶覆寫），雙擊 `automation/QuantCore_Panel.vbs`
4. **安裝排程**：執行 `python automation/install_all_tasks.py` 並核准 UAC 視窗（8 個排程，定義在腳本開頭；會移除已退役的舊任務）
5. **切換實盤**：確認無誤後，把 `dry_run` 改為 `false`（Panel 沒有切換開關）

---

## 開發

```bash
pip install -r requirements-dev.txt
pytest automation/tests/ -v            # 305+ 離線測試（不需 IB / 網路）
```

- 領域語言：[CONTEXT.md](CONTEXT.md)
- 協作規則：[AGENTS.md](AGENTS.md)
- Claude Code 入口：[CLAUDE.md](CLAUDE.md)（引用 `AGENTS.md`）
- 架構決策：[docs/adr/README.md](docs/adr/README.md)

`AGENTS.md` 是共用 AI 開發規範。Agent 與使用者對話預設使用白話繁體中文；Agent 建立的 GitHub Issue 預設使用繁體中文。

---

## 技術細節

| 項目 | 說明 |
|------|------|
| 資料來源 | yfinance（增量更新，追加至 CSV） |
| 券商 API | ib_insync（IB Gateway / TWS） |
| 通知 | Telegram Bot API（文字，urllib） |
| 交易紀錄 | Excel（trade_log / portfolio_tracker） |
| 權益圖表 | matplotlib 雙面板（權益曲線 + MDD） |
| 排程 | Windows Task Scheduler |
| GUI | Tkinter（深色主題） |
| CI | GitHub Actions（Python 3.11–3.13，windows-latest） |
| 融資模型 | IB Pro 分級利率，逐日計算（`backtest/leverage.py`） |
