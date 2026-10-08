# QuantCore — 新電腦環境設置指南

本文件用於在新電腦上完整複製 QuantCore 環境，並設定自動資料更新排程。

---

## 前置需求

- Windows 10/11
- Python 3.11+（建議從 python.org 安裝，勾選 "Add to PATH"）
- Git（git-scm.com）
- GitHub 帳號已有 TaylorYam/QuantCore 私有倉庫的存取權限

---

## Step 1：克隆程式碼

```powershell
cd C:\
git clone https://github.com/TaylorYam/QuantCore.git
cd QuantCore
```

---

## Step 2：建立虛擬環境並安裝套件

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> 若 PowerShell 提示執行原則錯誤，先執行：
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

---

## Step 3：下載並解壓縮歷史資料

1. 從 Google Drive 下載 `QuantCore_data.zip`（請向 Taylor 索取共用連結）
2. 解壓縮到 `C:\QuantCore\`

解壓縮後應出現以下目錄結構：
```
C:\QuantCore\
├── data\
│   ├── stocks\       ← 500+ 支個股 CSV（2015–present）
│   ├── etfs\         ← SPY, SSO, XLK, XLF ... VIX CSV
│   └── reference\    ← sp500_constituents_daily.xlsx
│                        us_trading_days.xlsx
```

---

## Step 4：驗證安裝

```powershell
# 進入虛擬環境
.\venv\Scripts\Activate.ps1

# 確認資料目錄
python -c "from pathlib import Path; p=Path('C:/QuantCore/data/etfs/SPY.csv'); print('OK' if p.exists() else 'MISSING')"

# 試跑資料更新（只更新 ETF，速度快）
python data/update_data.py --etf
```

正常輸出範例：
```
=======================================================
ETF 更新
=======================================================
  下載區間：2026-03-15 → 2026-03-20
  SPY     +3 筆  (至 2026-03-19)
  SSO     +3 筆  (至 2026-03-19)
  XLK     +3 筆  (至 2026-03-19)
  ...
```

---

## Step 5：設定 Windows 工作排程器（每週自動更新）

> 這一步是給研究用電腦的每週資料更新。實盤機器的完整排程（8 個任務）請改用 `python automation/install_all_tasks.py`。

以下指令會建立一個排程任務，每週一早上 07:00 自動執行完整資料更新。

**以系統管理員身份開啟 PowerShell**，然後執行：

```powershell
$action = New-ScheduledTaskAction `
    -Execute "C:\QuantCore\venv\Scripts\python.exe" `
    -Argument "C:\QuantCore\data\update_data.py" `
    -WorkingDirectory "C:\QuantCore"

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday `
    -At "07:00AM"

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask `
    -TaskName "QuantCore_WeeklyUpdate" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "QuantCore 每週一自動更新 ETF + 個股資料" `
    -RunLevel Highest
```

確認建立成功：
```powershell
Get-ScheduledTask -TaskName "QuantCore_WeeklyUpdate"
```

手動測試執行一次：
```powershell
Start-ScheduledTask -TaskName "QuantCore_WeeklyUpdate"
# 等候 30 秒後查看執行結果
Get-ScheduledTaskInfo -TaskName "QuantCore_WeeklyUpdate"
```

---

## Step 5.5：驗證資料一致性（選用）

若從另一台電腦同步資料，可用 checksum 工具比對：

```powershell
# 在來源電腦產生 checksum
python data/checksum.py
# 複製 data/checksums.sha256 到新電腦

# 在新電腦驗證
python data/checksum.py --verify

# 或直接比對兩份 checksum 檔
python data/checksum.py --compare C:\path\to\other\checksums.sha256
```

---

## Step 5.6：初始化 State 檔案

自動調倉需要帳戶狀態檔（不在 Git 中）。首次設置時從範本複製：

```powershell
cd C:\QuantCore\automation

# SP500 Momentum state（每帳戶一份）
copy sp500_state_example.json sp500_state_AccountA.json
copy sp500_state_example.json sp500_state_AccountB.json

# Skew LS state（每帳戶一份）
copy skewls_state_example.json skewls_state_AccountA.json
copy skewls_state_example.json skewls_state_AccountB.json
```

帳戶名稱需對應 `accounts.json` 中的 `name` 欄位。首次執行 rebalance 時若無 state 檔，系統會自動以預設值建立。

---

## Step 6：手動更新指令速查

```powershell
cd C:\QuantCore
.\venv\Scripts\Activate.ps1

# 更新全部（ETF + 個股）
python data/update_data.py

# 只更新 ETF（速度快，約 10 秒）
python data/update_data.py --etf

# 只更新個股（速度慢，約 5–15 分鐘）
python data/update_data.py --stocks
```

---

## Step 7：執行回測（選用）

```powershell
cd C:\QuantCore
.\venv\Scripts\Activate.ps1

# 執行 50/50 月度再平衡回測
python -m backtest.run_all

# 執行並產出 QuantStats HTML 報告
python -m backtest.run_all --quantstats

# 自訂配置（80% SR + 50% SP500 = 1.3x 槓桿）
python -m backtest.run_all --alloc 0.8 0.5 --quantstats
```

輸出位於 `C:\QuantCore\output\`：
- `equity_curves.png` — 資金曲線圖
- `qs_*.html` — QuantStats 完整分析報告

---

## 常見問題

| 問題 | 解決方式 |
|------|---------|
| `ModuleNotFoundError: yfinance` | 確認已啟動虛擬環境後再 `pip install -r requirements.txt` |
| `FileNotFoundError: SPY.csv` | 確認 QuantCore_data.zip 已正確解壓縮到 C:\QuantCore\ |
| PowerShell 執行原則錯誤 | `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` |
| 個股更新速度很慢 | 正常現象，500+ 支股票分批下載需 5–15 分鐘 |
| 某支 ETF 顯示 `ERROR` | 網路問題，重跑 `python data/update_data.py --etf` 即可 |
| 工作排程器沒有執行 | 確認電腦週一早上有開機；或改用 `Start-ScheduledTask` 手動觸發 |

---

## 目錄結構總覽

```
C:\QuantCore\
├── strategies\
│   ├── sector_rotation\        ← SR 板塊輪動策略
│   ├── sp500_momentum\         ← SP500 個股動量策略
│   └── sector_skew_ls\         ← Skew LS 偏態多空策略
├── automation\
│   ├── tests\                  ← 305+ 離線單元測試
│   ├── rebalance_plan.py       ← 調倉決策（純函式）
│   ├── rebalance_ib.py         ← IB 自動調倉
│   ├── signals.py              ← 三策略訊號整合
│   ├── qt_panel/               ← PySide6 控制面板（qt_panel_app.py 啟動）
│   ├── monitor_engine.py       ← 值班引擎
│   ├── accounts.json           ← 帳戶設定（gitignore，從 .example 複製）
│   └── config.json             ← 連線設定（gitignore，從 .example 複製）
├── backtest\
│   ├── run_all.py              ← 回測執行入口
│   ├── combo.py                ← 組合回測邏輯
│   ├── charts.py               ← 圖表生成
│   ├── leverage.py             ← IB 槓桿成本計算
│   └── metrics.py              ← 績效指標
├── robustness\                 ← WF / Monte Carlo / 參數敏感度研究
├── data\
│   ├── update_data.py          ← 增量更新腳本
│   ├── refresh_constituents.py ← SP500 成分股自動更新
│   ├── stocks\                 ← 個股 CSV（gitignore）
│   ├── etfs\                   ← ETF CSV（gitignore）
│   └── reference\              ← 成分股歷史 / 交易日曆（gitignore）
├── shared\                     ← 分享版工具（Dashboard / 回測）
├── docs\
│   ├── adr\                    ← 架構決策紀錄（ADR-0001 ~ 0011）
│   └── setup_new_computer.md   ← 本文件
├── output\                     ← 回測結果（gitignore）
├── CONTEXT.md                  ← 領域語言定義
├── AGENTS.md                   ← 協作規則
├── requirements.txt            ← Python 依賴
├── requirements-dev.txt        ← 開發依賴（含 pytest）
├── .github\workflows\ci.yml   ← CI 設定
└── .gitignore
```
