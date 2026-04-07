# QuantCore — Shared Edition

50/50 不開槓桿策略工具包，適用於透過**國內複委託**交易美股的用戶。

---

## 策略概要

本系統結合兩個動能策略，各配置 50% 資金（總曝險 100%，不使用槓桿）：

| 策略 | 配置 | 標的 | 再平衡日 |
|------|------|------|----------|
| **Sector Rotation MOM** | 50% | 11 檔 SPDR 板塊 ETF | 每週二 |
| **SP500 Momentum** | 50% | S&P 500 成分股（持有 4 檔）| 每週三 |

### 月初重設機制
- 每月第一個交易日，將兩策略的配置比例**重設回 50/50**
- 月內各策略的比重隨報酬自然漂移，不做日內再平衡
- 不使用融資，無槓桿成本

---

## Quick Start

```bash
cd C:/QuantCore-master

# 1. 安裝 Python 套件
pip install pandas numpy matplotlib yfinance openpyxl quantstats

# 2. 開啟統一儀表板（資料更新、倉位計算、回測全在這裡）
python -m shared.dashboard
```

所有操作都在 Dashboard 內完成，不需要記指令。

> 也可以單獨執行各工具（進階用法，見下方說明）。

---

## 工具一覽

### Dashboard `dashboard.py`（主入口）

統一儀表板，三個分頁整合所有功能：

```bash
python -m shared.dashboard
```

| 分頁 | 功能 |
|------|------|
| **Portfolio** | 輸入 NAV + 持倉 → 載入策略訊號 → 計算目標持倉與操作清單 |
| **Backtest** | 設定手續費 → 一鍵回測 → 績效表 + 權益曲線 + QuantStats 報告 |
| **Data Update** | 查看資料狀態 → 一鍵更新 ETF / 個股（yfinance）|

---

### 以下為單獨執行的進階用法：

### 1. 回測工具 `run_backtest.py`

執行 50/50 策略回測，支援自訂手續費率，產出績效表、權益曲線圖、QuantStats HTML 報告。

```bash
cd C:/QuantCore-master

# 基本回測（使用預設手續費 0.075%）
python -m shared.run_backtest

# 指定複委託手續費（單向 0.3%）
python -m shared.run_backtest --commission 0.003

# 加上 QuantStats 深度分析報告
python -m shared.run_backtest --commission 0.003 --quantstats

# 指定回測區間
python -m shared.run_backtest --start 2020-01-01 --end 2025-12-31 --commission 0.002

# 指定初始本金
python -m shared.run_backtest --commission 0.003 --equity 500000 --quantstats
```

#### 手續費參數 `--commission`

此為**單向**交易成本（買進 or 賣出各收一次），包含券商手續費 + 預估滑點。

| 情境 | 費率 | 參數值 |
|------|------|--------|
| IB Pro（系統預設）| 0.075% | `0.00075` |
| 複委託低費率 | 0.15% | `0.0015` |
| 複委託折扣後 | 0.20% | `0.002` |
| 複委託一般牌告 | 0.30% | `0.003` |
| 較高費率 | 0.50% | `0.005` |

#### 輸出檔案

所有輸出存放於 `output/shared/` 資料夾：
- `equity_curve_5050_tc{N}bps.png` — 權益曲線圖（Log Scale）
- `tearsheet_*.html` — QuantStats 完整績效報告（需加 `--quantstats`）

---

### 2. 資料更新 `update_data.py`

透過 yfinance 從 Yahoo Finance 下載最新收盤資料。**不需要 IB 帳號**，也不需要設定 Telegram。

```bash
cd C:/QuantCore-master

# 更新全部（ETF + 個股）
python -m shared.update_data

# 只更新 ETF（較快）
python -m shared.update_data --etf

# 只更新個股（~500 支，較慢）
python -m shared.update_data --stocks
```

#### 更新時機
- **每個交易日**美股收盤後執行一次
- 台灣時間約**早上 05:00 以後**（夏令時間 04:30 以後）
- 系統會自動偵測上次更新日期，只下載新資料（增量更新）

#### 重要提醒
- 首次取得系統時，`data/` 資料夾已包含完整歷史資料
- 之後只需每日執行增量更新，通常 ETF 幾秒完成、個股約 2-5 分鐘
- 如果好幾天沒更新也沒關係，下次執行會自動補齊缺漏的交易日
- 回測和倉位計算器都依賴 `data/` 裡的 CSV，**請務必保持資料更新**

---

### 3. 倉位計算器 `portfolio_calc.py`

圖形化工具，輸入你的帳戶淨值與當前持倉，即可計算目標倉位與操作清單。

```bash
cd C:/QuantCore-master
python -m shared.portfolio_calc
```

#### 使用步驟

1. **輸入帳戶淨值 (NAV)**：你的複委託帳戶美元淨值
2. **確認配置比例**：預設 ETF 50% + Stocks 50%（可調整）
3. **點擊「Load Strategy Signals」**：載入最新策略訊號
4. **輸入當前持倉**：填入 Symbol 和股數（或從 Excel 貼上）
5. **查看操作清單**：依照 BUY / SELL / HOLD 指示下單

#### 再平衡時機

| 時間 | 動作 |
|------|------|
| 每週二 | 檢查 ETF Sector Rotation 訊號，調整 ETF 部位 |
| 每週三 | 檢查 SP500 Momentum 訊號，調整個股部位 |
| 每月初 | 檢查 ETF vs Stock 配置比例，重設回 50/50 |

---

## 環境需求

```
Python >= 3.10
pandas
numpy
matplotlib
yfinance          # 資料更新（Yahoo Finance）
openpyxl          # 讀取 S&P500 成分股 Excel
quantstats        # 選用，產生 HTML 報告
```

安裝：
```bash
pip install pandas numpy matplotlib yfinance openpyxl quantstats
```

---

## 與正式版的差異

| 功能 | 正式版 | Shared Edition |
|------|--------|----------------|
| 策略配置 | 80/50（1.3x 槓桿）| 50/50（無槓桿）|
| IB 自動下單 | 有 | 無（手動操作）|
| IB 融資成本 | 自動計算 | 不適用 |
| 手續費 | 固定 IB 費率 | 可自訂（複委託）|
| 資料更新 | 排程自動 + Telegram 通知 | 手動執行 `python -m shared.update_data` |
| Telegram 通知 | 有 | 無 |
| 倉位計算器 | 含 IB 即時報價 | 使用本地 CSV 收盤價 |

---

## FAQ

**Q: 我的複委託手續費是 0.25%，要怎麼設定？**
```bash
python -m shared.run_backtest --commission 0.0025 --quantstats
```

**Q: 月中才開始，要怎麼建倉？**
開啟倉位計算器，輸入你的 NAV，載入訊號後按照操作清單建倉即可。系統會在下個月初自動將目標比例重設為 50/50。

**Q: ETF 和個股的再平衡日不同怎麼操作？**
ETF 在週二調整，個股在週三調整。建議每週二、三各開一次計算器載入訊號確認。

**Q: 如果 SPY 跌破 200 日均線怎麼辦？**
系統會偵測熊市狀態，個股部位（SP500 Momentum）會建議全部清空。ETF 部位照常運作。

**Q: 資料要怎麼更新？多久更新一次？**
每個交易日收盤後執行 `python -m shared.update_data`，通常 2-5 分鐘完成。資料來自 Yahoo Finance（yfinance），不需要 IB 帳號。忘記更新也沒關係，下次執行會自動補齊。

**Q: 資料夾 `data/` 裡面有什麼？可以刪嗎？**
`data/etfs/` 放 14 檔 ETF 的歷史 OHLCV（SPY, SSO, QQQ + 11 板塊），`data/stocks/` 放 ~500 檔 S&P500 成分股，`data/reference/` 放成分股名單等參考資料。**請勿刪除**，所有功能都依賴這些資料。
