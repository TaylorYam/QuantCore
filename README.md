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
├── shared/                     # 分享版工具（Dashboard / 回測 / 資料更新）
├── docs/
│   ├── adr/                    #   架構決策紀錄（ADR-0001 ~ 0011）
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

目前三帳戶配置（z-family，WF + Monte Carlo 驗證定案）：

| 帳戶 | alloc_sr | alloc_sp500 | alloc_skewls | Leverage |
|------|----------|-------------|--------------|----------|
| Yvonne | 0 | 0.40 | 1.00 | 1.4x |
| Rosa | 0 | 0.70 | 1.00 | 1.7x |
| Taylor | 0 | 0.80 | 1.00 | 1.8x |

核心特徵：100% Skew LS + SP500 個股動量作為 margin stock，SR 停用。詳見 [CONTEXT.md](CONTEXT.md) 的 Allocation / Leverage 定義與 [ADR-0001](docs/adr/0001-alloc-skew-factor-exposure.md)。

---

## 自動化流程

```
台灣時間          動作                         說明
──────────────────────────────────────────────────────────
04:30  每日       資料更新                      yfinance 抓取最新收盤價
04:40  每日       持倉報告 → Telegram           NAV、損益、權益曲線圖
04:50  週三       Constituents 自動 refresh     gated 寫入 + diff 報告（ADR-0009）
07:00  交易日     Morning check                 健檢 + constituents staleness 提醒
16:00  交易日     SP500 Daily Check             熊市 dip-buy 觸發（LMT outsideRTH）
20:05  週二/三    TY check                      驗證 Gateway 手動登入
21:00  週二       Tue Rebalance (--mode all)     三策略合併：SR + SP500 hold + SkewLS check
21:00  週三       Wed Rebalance (--mode all)     三策略合併：SR hold + SP500 + SkewLS check
21:00  週一/四/五 SkewLS Check (--mode skewls)   獨立 threshold check（Tue/Wed 由上方涵蓋）
21:00–05:00      Rosa poll                      Gateway 連線狀態 30 分鐘輪詢
21:31  週二/三    MOO 成交回報 → Telegram        開盤後 1 分鐘檢查成交
```

- **MOO（Market-on-Open）**：保證以開盤價成交，須在美東 09:28 前送出
- 台灣 21:00 = 美東 09:00（夏令），留有 28 分鐘緩衝

---

## 控制面板

Tkinter GUI 儀表板（`QuantCore_Panel.vbs` 靜默啟動）：

- **排程管理**：安裝 / 啟用 / 停用 Windows Task Scheduler 任務
- **Dry Run 切換**：模擬模式 ↔ 實盤模式
- **三帳戶管理**：Taylor / Rosa / Yvonne 各帳戶獨立配置
- **手動觸發**：Tue Rebalance (SR+Skew) / Wed Rebalance (SP500+Skew) / Report / Data / Sync Pos
- **Gateway 監控**：系統匣圖示顯示連線狀態（綠/紅/灰）
- **即時 Log**：右側面板即時串流腳本輸出

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
3. **Dry Run 測試**：確保 `dry_run: true`，雙擊 `QuantCore_Panel.vbs`
4. **安裝排程**：控制面板 → Install All Tasks
5. **切換實盤**：確認無誤後切換為 LIVE

---

## 開發

```bash
pip install -r requirements-dev.txt
pytest automation/tests/ -v            # 305+ 離線測試（不需 IB / 網路）
```

- 領域語言：[CONTEXT.md](CONTEXT.md)
- 協作規則：[AGENTS.md](AGENTS.md)
- 架構決策：[docs/adr/README.md](docs/adr/README.md)

---

## 技術細節

| 項目 | 說明 |
|------|------|
| 資料來源 | yfinance（增量更新，追加至 CSV） |
| 券商 API | ib_insync（IB Gateway / TWS） |
| 通知 | Telegram Bot API（文字 + 圖片，urllib） |
| 交易紀錄 | Excel（trade_log / portfolio_tracker） |
| 權益圖表 | matplotlib 雙面板（權益曲線 + MDD） |
| 排程 | Windows Task Scheduler |
| GUI | Tkinter（深色主題） |
| CI | GitHub Actions（Python 3.11–3.13，windows-latest） |
| 融資模型 | IB Pro 分級利率，逐日計算（`backtest/leverage.py`） |
