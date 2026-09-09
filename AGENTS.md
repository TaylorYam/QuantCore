# AGENTS.md — AI Agent Collaboration Rules

本文件是所有 AI CLI（Claude Code、Cursor、Copilot 等）在 QuantCore 上工作的共同規則。
人類開發者同樣適用分支與 PR 規範。

## 1. 開始工作前必讀

| 文件 | 用途 |
|------|------|
| `CONTEXT.md` | 領域語言——所有名詞以此為準，不要自創同義詞 |
| `docs/adr/README.md` | 架構決策索引——被否決的提案不要重提 |
| `automation/tests/conftest.py` | 測試設計原則（離線、無 IB、無真實 config） |
| 本文件 | 協作規則 |

## 2. 分支與 PR 工作流

- **主線**：`main`（`origin/master` 是歷史 stub，不要在上面開發）
- **一個任務 = 一個 Issue → 一個分支 → 一個 PR**
- 分支命名：`<type>/<slug>`，例如 `fix/rosa-poll-timeout`、`feat/ci-pipeline`、`refactor/signal-seam`
- PR 目標分支：`main`
- 合併前必須：CI 綠燈 + 人類 approve
- ADR 編號只在 main 上分配——分支上撞號以 main 為準重編

## 3. 絕對禁止

- **不改 live 配置**：`automation/accounts.json` 和 `automation/config.json` 中的 `alloc_*` / `leverage` / `port` / `account` 欄位絕不修改。測試用 monkeypatch（見 conftest.py），不動檔案。
- **不連 IB**：不執行任何會連線 IB Gateway 的指令（`rebalance_ib.py`、`sync_position.py --live` 等）。
- **不發 Telegram**：不觸發真實 Telegram 通知。
- **不推論策略行為**：策略宣稱必須有回測/WF/logs 數據佐證，不接受純推理結論。
- **不重提被否決的 ADR**：ADR-0006（sector netting）、0007（min-position）、0008（weight-level extraction）、0010（take-profit exit）已由數據否決，不要重新提案或以不同名稱包裝同一概念。

## 4. 部署模型

QuantCore 的 live 部署 = Windows Task Scheduler 直接跑 `C:\QuantCore-master\automation\*.py`。
**工作目錄裡的檔案就是 live code**——沒有 build/deploy 步驟。

後果：
- 改一行 `.py` = 立即部署（下次排程就跑新版）
- `git checkout` 會改寫工作目錄 = 切換 live 行為
- **不要在 live 機器的 main 工作目錄做實驗性修改**，用 worktree 或本地分支

## 5. 測試要求

### 所有 PR
```
pytest automation/tests/ -v
```
305+ tests，全部離線，不需 IB/網路/真實 config。CI 會自動跑。

### 行為等價改動（重構、合併、模組搬移）
除了 pytest 全綠，還需要 characterization test：
1. 改動前抓 OLD 基準（3 帳戶 × 5 情境的 signal 輸出 + state 檔）
2. 改動後抓 NEW，要求 byte-identical diff
3. State 檔在每輪測試前後備份還原

### 策略行為改動
- 必須有完整回測 A/B 比較
- 設計變更需過 Walk-Forward（Pardo N=8 + N±1）
- 數據先行，建議後行

## 6. 並行協作

- 同一台電腦可開多個 CLI，各用不同 worktree + 不同分支
- 不同電腦各自 clone，各用自己的分支
- **同一任務分支同時只能有一個寫入者**
- 優先分配不同模組；兩分支改同一檔案不是 Git 禁止，但會增加衝突
- 預設等依賴 PR 合回 main 後再更新——不要 rebase 別人正在用的分支

### Worktree 隔離注意事項
Worktree 只隔離程式碼。以下**不會被隔離**，需各自處理：
- `data/`（股價 CSV、reference）— 共用只讀
- `automation/sp500_state_*.json`、`skewls_state_*.json` — runtime state
- `automation/*.xlsx` — Excel 報表
- `automation/logs/` — 日誌
- IB Gateway ports、Windows Task Scheduler 排程
- 實盤工作目錄（`C:\QuantCore-master`）應獨立於開發 worktree

## 7. 資料與機密

| 路徑 | 性質 | Git 追蹤？ |
|------|------|-----------|
| `automation/accounts.json` | 帳號 + alloc（機密） | .gitignored |
| `automation/config.json` | API token + 連線設定（機密） | .gitignored |
| `data/stocks/`, `data/etfs/`, `data/reference/` | 市場資料 | .gitignored |
| `automation/sp500_state_*.json` | Runtime state | .gitignored |
| `automation/skewls_state_*.json` | Runtime state | .gitignored |
| `output/` | 回測產出 | .gitignored |
| `automation/logs/` | 執行日誌 | .gitignored |

**永遠不 commit 機密或個人資料。** 範本見 `accounts.example.json` / `config.example.json`。

## 8. Commit 訊息

```
<type>(<scope>): <summary>

<body — optional>
```

Type: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`
Scope: `sp500`, `sr`, `skewls`, `automation`, `backtest`, `data`, `robustness`, `ci`

參考 `git log --oneline -20` 的既有風格。

## 9. 新增 ADR

1. 在分支上寫 `docs/adr/NNNN-<slug>.md`，編號暫定
2. 更新 `docs/adr/README.md` 索引
3. 合回 main 時若撞號，以 main 為準重編
4. 被否決的提案也要記錄（`rejected`），附上否決的數據依據
