# AI Development Guide

本文件是 QuantCore 的主要 AI development policy，適用所有 AI CLI（Claude Code、Cursor、Copilot 等）；人類開發者同樣適用分支與 PR 規範。本文件定義何時使用各 workflow；Skills 定義如何執行。使用者明確指定的任務範圍與審核界線優先。

## Work routing

先判斷工作路線，再建立 Issue：

- 新功能，重要需求不明：`/grill-with-docs` → `/to-spec` → 工作過大時才用 `/to-tickets` → `/implement` → `/code-review`。只追問真正影響決策的不確定處。
- 新功能，需求清楚：檢查相關程式 → 建立 GitHub Issue → `/implement <issue>` → `/code-review`。
- Bug，原因與正確行為清楚：檢查相關程式 → 建立 GitHub Issue → `/implement <issue>` → `/code-review`；合理時補回歸測試。
- Bug，原因不明：`/diagnosing-bugs` → 重現問題 → 找出根因 → 確認正確行為 → 建立 GitHub Issue → `/implement <issue>` → `/code-review`。區分假設與已證實的原因。
- 外部或他人建立的 Issue：既有 Issue → `/triage <issue>` → `ready-for-agent` → `/implement <issue>` → `/code-review`。

`/triage` 主要處理他人建立的 Issue；agent、`/to-spec` 或 `/to-tickets` 建立且已可實作的 Issue 不必重複 triage。只有單一 Issue／PR 過大時才用 `/to-tickets`。AGENTS.md 定義何時使用 workflow；Skills 定義具體做法。

## Before changing files

- 檢查目前分支與 working tree。一般開發工作從最新 `main` 建立任務分支或 worktree，再修改檔案；注意下方 live 部署模型。
- 若有 Issue，先讀 Issue；再讀 `README.md`、`CONTEXT.md`、相關 ADR、實作與測試。領域名詞以 `CONTEXT.md` 為準。
- 重大工作先說明預期成果、可能修改的檔案、做法、風險與驗證方式，遵守使用者明確指定的審核界線。
- Issue 或 spec 若未決定會實質影響策略／交易行為、資料正確性、架構、安全、API、相容性或刪除資料的重要選擇，停在規劃階段，以白話繁體中文請使用者決定。低風險且可逆的實作細節自行處理。

## GitHub-first workflow

一般規劃工作依 `Issue → branch／worktree → plan → implementation → validation → commit and push → PR → review and CI → merge` 進行；使用者指定停在 review、不要 commit／push／開 PR 時，就停在該界線。

- 實作前建立可執行的 GitHub Issue，寫清楚目標、範圍與可觀察的驗收條件。Agent 建立的 Issue 標題與本文各段預設用臺灣用語的繁體中文；正式技術識別字保留英文。既有英文 Issue 不必重寫；與英文使用者直接溝通時可用英文。
- Issue 與 spec 使用本 repository 的 GitHub Issues；操作方式見 `docs/agents/issue-tracker.md`。Triage 標籤對照見 `docs/agents/triage-labels.md`。
- 變更聚焦 Issue；PR 連結 Issue，記錄重要設計取捨與未完成事項。執行相關檢查，說明無法執行的檢查；review 與 CI 通過後才合併。
- 下方 QuantCore 分支、PR、測試與實盤限制是上述共用流程的專案特定要求。

## Communication

- 與使用者日常對話預設使用簡明、具體、白話的繁體中文（臺灣用語），包括 `/grill-with-docs` 的提問；深入問題也用日常語言表達。
- 必要時使用技術詞，陌生詞第一次出現時簡短解釋。保留函式、類別、API、函式庫、路徑及指令原名。

## Project-specific rules

以下保留 QuantCore 原有的分支、實盤安全、測試、部署、資料與 ADR 規則；與上方共用規則同時適用，較具體的 QuantCore 限制優先。

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

**永遠不 commit 機密或個人資料，也不把機密寫入 logs 或範例。** 範本見 `accounts.example.json` / `config.example.json`。機器專屬設定、產生資料、快取與執行輸出依現有 `.gitignore` 保留在 Git 之外；若在 working tree 或歷史中發現機密，停止處理並回報，不複製其內容。

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

## Design and implementation

- 遵循既有架構、命名、格式與依賴；以符合驗收條件的最小完整變更實作。行為、環境設定或介面改變時更新相關文件；長期架構決策依下方 ADR 流程記錄。
- QuantCore 的領域術語以 `CONTEXT.md` 為準，並查閱相關 ADR；若與既有 ADR 衝突，先提出衝突，不自行覆寫。

## Completion

檢查 diff、執行適用驗證，交回時說明變更、測試結果與限制；保留無關的既有修改。Commit、push、PR 與合併仍須遵守使用者指定的審核界線及上方 QuantCore 規則。

## Agent skills

### Issue tracker

Issues and specs live in this repository's GitHub Issues; use the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repo. Read root `CONTEXT.md` and relevant ADRs under `docs/adr/`. See `docs/agents/domain.md`.
