# QuantCore — Taylor

雙策略量化交易系統 — 結合 Sector Rotation 動量策略與 S&P 500 個股動量策略，透過 Interactive Brokers 自動化執行。

> Built by Taylor | Automated Quant Trading on IB Gateway

---

## 系統架構

```
QuantCore-master/
├── strategies/             # 策略引擎
│   ├── sector_rotation/    #   板塊輪動動量 (SR MOM)
│   └── sp500_momentum/     #   S&P 500 個股動量
├── backtest/               # 回測框架（組合、績效、圖表、槓桿模型）
├── data/                   # 市場資料
│   ├── etfs/               #   板塊 ETF 日線 CSV
│   ├── stocks/             #   S&P 500 成分股日線 CSV（500+）
│   └── reference/          #   成分股歷史 / 交易日曆
├── automation/             # 自動化交易 + 儀表板
│   ├── control_panel.py    #   Tkinter GUI 控制面板
│   ├── rebalance_ib.py     #   IB Gateway 自動調倉
│   ├── moo_fill_check.py   #   MOO 開盤成交回報（開盤後 1 分鐘）
│   ├── daily_report.py     #   每日持倉報告 + 權益曲線圖 → Telegram
│   ├── equity_chart.py     #   權益曲線 + MDD 圖表產生器
│   ├── update_data.py      #   收盤後資料增量更新
│   ├── excel_logger.py     #   交易紀錄 / 每日快照寫入 Excel
│   └── config.json         #   連線 / 配置中心
└── output/                 # 回測報告（權益曲線、QuantStats HTML）
```

---

## 策略簡介

### 1. Sector Rotation MOM（板塊輪動）

| 項目 | 設定 |
|------|------|
| 標的池 | 11 支 SPDR 板塊 ETF（XLK、XLF、XLV、XLE 等） |
| 訊號 | 多週期加權動量（5/10/20/60 日，權重 0.4/0.3/0.2/0.1） |
| 進場條件 | Z-score > 0.4 |
| 持倉權重 | 圓整至 5% |
| 調倉頻率 | 每週二 |

### 2. S&P 500 Momentum（個股動量）

| 項目 | 設定 |
|------|------|
| 標的池 | S&P 500 成分股（動態追蹤歷史成分變動） |
| 訊號 | 90 日動量排名，ATR 反比例加權 |
| 持股數 | 4 檔 |
| Rank Buffer | 排名跌出 Top 20 才賣出，避免頻繁換股 |
| 停損 | -20% 停損 / 跌破 EMA50 / 過熱保護（adj_slope > 1.5） |
| 調倉頻率 | 每週三 |
| 熊市機制 | SPY MA200 偵測，觸發時轉入 SPY 抄底 |

### 組合配置

預設配置：**80% SR + 50% SP500 = 130% 曝險（1.3x 槓桿）**

透過 IB 保證金融資實現超額曝險，融資成本依 IB Pro 分級利率逐日扣除。

---

## 自動化流程

```
台灣時間         動作                     說明
─────────────────────────────────────────────────────
04:30  每日      資料更新                  yfinance 抓取最新收盤價
04:40  每日      持倉報告 → Telegram       NAV、損益、權益曲線圖
21:00  週二      SR 調倉 → IB Gateway      MOO 單（開盤前送出）
21:00  週三      SP500 調倉 → IB Gateway   MOO 單（開盤前送出）
21:31  週二/三   MOO 成交回報 → Telegram   開盤後 1 分鐘檢查成交
```

- **MOO（Market-on-Open）**：保證以開盤價成交，須在美東 09:28 前送出
- 台灣 21:00 = 美東 09:00（夏令），留有 28 分鐘緩衝
- **開盤回報**：21:31（美東 09:31）自動連線 IB 檢查 MOO 委託成交狀況

---

## 控制面板

Tkinter GUI 儀表板（`QuantCore_Panel.vbs` 靜默啟動）：

- **排程管理**：安裝 / 啟用 / 停用 Windows Task Scheduler 任務（6 個）
- **Dry Run 切換**：模擬模式 ↔ 實盤模式，一鍵切換
- **參數調整**：SR / SP500 配置比例滑桿，即時顯示總曝險
- **手動觸發**：SR / SP500 / Both / Report / Data 一鍵執行
- **即時 Log**：右側面板即時串流腳本輸出，含時間戳和執行狀態

---

## 回測

```bash
cd C:/QuantCore-master

# 基本回測（100/0、0/100、50/50）
python -m backtest.run_all

# 自定義配置 + QuantStats 報告
python -m backtest.run_all --alloc 0.8 0.5 --quantstats

# 指定起始日
python -m backtest.run_all --start 2019-01-01 --quantstats
```

輸出：
- `output/equity_curve*.png` — 權益曲線（對數座標）
- `output/tearsheet_*.html` — QuantStats 完整績效報告

---

## 快速開始

### 環境需求

- Python 3.11+
- Windows（Task Scheduler 排程）
- Interactive Brokers Gateway（Port 7497）

### 安裝

```bash
pip install pandas numpy yfinance ib_insync matplotlib quantstats openpyxl
# Optional — accurate NYSE holiday awareness for the ROSA market-window
# poller. If omitted, control_panel falls back to a hardcoded 2026-2027
# NYSE holiday list (warning logged at startup).
pip install pandas_market_calendars
```

### 設定

編輯 `automation/config.json`：

```json
{
  "host": "127.0.0.1",
  "port": 7497,
  "account": "YOUR_ACCOUNT_ID",
  "dry_run": true,
  "order_type": "MOO",
  "alloc_sr": 0.80,
  "alloc_sp500": 0.50,
  "leverage": 1.3,
  "telegram_token": "YOUR_BOT_TOKEN",
  "telegram_chat_id": "YOUR_CHAT_ID"
}
```

### 啟動

1. **回測驗證**：`python -m backtest.run_all --alloc 0.8 0.5 --quantstats`
2. **更新資料**：`python automation/update_data.py`
3. **Dry Run 測試**：確保 `dry_run: true`，雙擊 `QuantCore_Panel.vbs`，點 Trigger Now
4. **安裝排程**：控制面板 → Install All Tasks
5. **切換實盤**：確認無誤後，控制面板 → SIMULATED 按鈕切換為 LIVE

---

## 技術細節

| 項目 | 說明 |
|------|------|
| 資料來源 | yfinance（增量更新，追加至 CSV） |
| 券商 API | ib_insync（IB Gateway / TWS） |
| 通知 | Telegram Bot API（文字 + 圖片，urllib，無額外套件） |
| 交易紀錄 | Excel（trade_log.xlsx / portfolio_tracker.xlsx） |
| 權益圖表 | matplotlib 雙面板（權益曲線 + MDD），每日自動產生 |
| 排程 | Windows Task Scheduler + PowerShell（6 個任務） |
| GUI | Tkinter（深色主題、Admin 自動提權） |
| 融資模型 | IB Pro 分級利率（5.14% → 4.39%），逐日計算 |
