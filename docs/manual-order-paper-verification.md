# 手動下單盤前盤後：Paper Account 驗證步驟（#104 / PR #116）

離線測試證明不了 IB 真正的行為。這份流程用 **IB paper 帳戶**實際下單，確認以下四件事：

| # | 要確認的事 | 對應 PR 風險 |
|---|---|---|
| A | IB 警告 2109（忽略盤前盤後設定）是否在這張單的第一個狀態回報之前，或回報後 5 秒確認窗內送達 | 殘餘風險 1：確認窗之後才到會漏掉 |
| B | 正常的盤前／盤後限價單：IB 接受、保留 outsideRth，而且**沒有** 2109 | 基本功能 |
| C | 「強制取消」從新的連線重送時，IB 會接受並確認取消 | 取消未確認時的重送機制 |
| D | 盤前單會延續到當天一般時段，並在美東 20:00 後失效、不跨日；IB 到期時回報的原始狀態 | DAY 效期、通知字樣 |

## 安全界線（先讀）

- **只連 paper Gateway。** 腳本連上後會檢查 Gateway 管理的帳號。只要有任何一個不是 `DU`／`DF` 開頭（live 帳號是 `U` 開頭），就立刻斷線並拒絕執行，連錯 port 也不會下到 live。
- **不讀也不改** `accounts.json`／`config.json`。host、port、帳號、clientId 全部從命令列給。
- **不發 Telegram。** 通知只印在畫面上。通知標題顯示「Taylor」只是服務要求的標籤，實際送往的是你給的 paper 帳號。
- **不碰 live 的委託紀錄。** 使用獨立的 store：`%TEMP%\quantcore_manual_orders_paper.sqlite3`。
- 所有輸出也會附加寫入 `%TEMP%\quantcore_manual_orders_paper.log`（UTF-8）。跑完告訴我，我直接讀這個檔。
- 在 PR 的 worktree 執行，**不要**在 `C:\QuantCore-master` 執行。
- live 的 3 個 Gateway 都是手動啟動、不會自己重連。所以登入 paper 後，一定要確認它們還連著（見步驟 0-3），而且避開排程前後的時段。

## 時間對照（台北時間；美國夏令時間到 2026-11-01 為止，之後全部 +1 小時）

| 美東 | 台北 | 說明 |
|---|---|---|
| 04:00–09:29 | 當天 16:00–21:29 | 盤前（程式在 09:29 停收） |
| 09:30–16:00 | 當天 21:30–隔天 04:00 | 一般時段 |
| 16:00–19:59 | 隔天 04:00–07:59 | 盤後（程式在 19:59 停收） |
| 20:00 | 隔天 08:00 | DAY 單到期 |

建議在台北 **17:00–20:30** 做盤前測試：避開 16:00 日度風控、21:00 調倉和 21:31 成交回報。盤後測試在台北 **04:45–07:30**：避開 04:30 資料更新和 04:40 每日報告。

## 0. 準備（任何時間）

1. **確認 port 沒被 live Gateway 用掉。** 在 PowerShell 執行：
   ```powershell
   netstat -ano | findstr LISTENING | findstr ":400"
   ```
   paper 預設用 4002。如果 4002 已經被占用，paper Gateway 改設其他 port（例如 4012），下面的指令也加上 `--port 4012`。
2. **另開一個 IB Gateway，登入模式選「Paper Trading」。** API 設定：
   - 勾選 Enable ActiveX and Socket Clients
   - **取消**勾選 Read-Only API
   - Socket port 設為上一步決定的 port
   - Trusted IPs 包含 127.0.0.1
3. **確認 live 三個 Gateway 仍連線。** 看 Panel 的 Gateway 狀態，或各 Gateway 視窗。如果有任何一個斷線，立刻登出 paper，先把 live 恢復，再決定要不要改天做。
4. **設定環境並跑合約交易時間檢查：**
   ```powershell
   cd C:\QuantCore-worktrees\manual-ext-hours-104
   $acct = "DU1234567"   # 換成你的 paper 帳號
   python automation\paper_verify_manual_orders.py --account $acct hours --symbol AAPL
   ```
   **預期結果：**
   - `tradingHours` 是 `YYYYMMDD:0400-YYYYMMDD:2000` 這類格式，`timeZoneId` 是美東時區。
   - 非交易時段執行時，`extended_session_at` 為 `None`。

   **如果出現「無法解析（會被擋單）」：** 先停下來把 log 給我。這代表正式環境的盤前盤後單會全部被擋。

## 1. 盤前（台北 17:00–20:30）

| 步驟 | 指令 | 預期 |
|---|---|---|
| 1-1 | `... hours --symbol AAPL` | `extended_session_at = PRE_MARKET`；`ib_trading_hours_include = True` |
| 1-2 | `... probe-2109 --symbol AAPL`，**做 3 次** | 每次印出 `PASS` 或 `PASS（確認窗內）`，並取消 probe 單。出現 `FAIL` 就停，把 log 給我。印出「未出現 2109」代表 paper 沒觸發，記下即可 |
| 1-3 | `... submit --session PRE_MARKET --symbol AAPL --quantity 1 --limit <現價約 7 折>`，輸入 `YES` | 送出約需 5–10 秒（等 IB 回報再加 5 秒確認窗）；狀態 `ACCEPTED`、「盤前盤後設定：已確認」；時間軸**沒有** `code=2109`；確認畫面寫「交易時段：盤前…延續至當日一般時段與盤後…不跨日」 |
| 1-4 | `... status` | 「IB 端原始委託」中，這張單是 `outsideRth=True tif=DAY status=Submitted`（若是 `PreSubmitted` 也記下來） |
| 1-5 | `... cancel <編號前 5 碼>`，接著 `... status` | 先 `CANCEL_PENDING`，再 `CANCELLED`；印出的通知有「盤前 DAY」「結果：已取消」 |
| 1-6 | 再 `submit` 一張同樣的遠價單，然後 `... force-retry <編號>`，再 `... status` | 時間軸在新連線上看到取消；最後 `CANCELLED`；通知多一行「自動取消原因：paper 驗證…」 |
| 1-7 | 再 `submit` 一張遠價單當**保留單 P**，**不要取消** | `ACCEPTED`（留給步驟 2、4） |
| 1-8（可選） | 台北 21:29:00–21:29:59 送 `submit --session PRE_MARKET …` | 預覽被拒：「…距離時段結束不到 1 分鐘…不會改用一般時段送出」 |

上表中 `...` 代表 `python automation\paper_verify_manual_orders.py --account $acct`。委託編號可以用 `... list` 查，輸入前幾碼即可。

## 2. 一般時段開盤後（台北 21:35 之後）

| 步驟 | 指令 | 預期 |
|---|---|---|
| 2-1 | `... status` | 保留單 P 仍是 `ACCEPTED`，IB 端 `Submitted`：盤前單確實延續到一般時段 |

## 3. 盤後（隔天台北 04:45–07:30）

| 步驟 | 指令 | 預期 |
|---|---|---|
| 3-1 | `... hours --symbol AAPL` | `AFTER_HOURS`；`True` |
| 3-2 | `... submit --session AFTER_HOURS --symbol AAPL --quantity 1 --limit <現價約 7 折>` 當**保留單 A** | `ACCEPTED`；沒有 2109；確認畫面寫「交易時段：盤後」 |
| 3-3（可選） | `... submit --session AFTER_HOURS --symbol AAPL --quantity 1 --limit <略高於賣價>` | 在 paper 成交後，`status` 顯示 `FILLED`，通知有「盤後 DAY」「結果：已成交」。之後用 `--action SELL`、限價略低於買價賣回 |

MKT 拒絕、美元金額換算、放空與回補限制，都已經有離線測試涵蓋，paper 上不用重做。

## 4. 到期（隔天台北 08:05 之後）

| 步驟 | 指令 | 預期 |
|---|---|---|
| 4-1 | `... status` | 保留單 P 和 A 都變成終態。**請特別看「IB 端原始委託」的 status**（`Cancelled`、`Expired` 或 `Inactive`），以及服務顯示的是「已取消」還是「已失效」。這關係到通知字樣要不要調整 |
| 4-2 | 下一個交易日台北 16:00 之後再 `... status` | P 和 A 都沒有再以 open 的狀態出現：確認不跨日 |

## 5. 收尾

1. 執行 `... list`，把還沒結案的委託都 `cancel` 掉，再用 `status` 確認。
2. 登出 paper Gateway，並確認 live 三個 Gateway 仍連線。
3. 告訴我「paper 驗證跑完了」。我會讀 `%TEMP%\quantcore_manual_orders_paper.log` 整理結果，並回填 PR。

## 判定方式

- **A 通過：** probe 至少有一次真的出現 2109，而且每一次都是 PASS 或 PASS（確認窗內）。任何一次 FAIL，就代表 2109 可能在確認窗之後才到，需要改設計（例如改用 IB 端的到期時間），不能 approve。
- **B、C、D 通過：** 結果和上面各表的「預期」一致。
- **Paper 的限制：** paper 是 IB 的模擬撮合，路由和警告不保證和真實帳戶完全相同。paper 通過會降低風險，但不等於證明 live 的行為。合併上線後，第一次真正使用時建議用 1 股、遠離市價的盤前或盤後限價單觀察一次。

## 驗證記錄（可直接填）

| 步驟 | 台北時間 | 結果（PASS／FAIL／備註） |
|---|---|---|
| 0-4 hours | | |
| 1-2 probe #1 | | |
| 1-2 probe #2 | | |
| 1-2 probe #3 | | |
| 1-3 盤前送單 | | |
| 1-4 IB 端 outsideRth／status | | |
| 1-5 取消 | | |
| 1-6 強制取消重送 | | |
| 2-1 盤前單延續到一般時段 | | |
| 3-2 盤後送單 | | |
| 4-1 到期時 IB 原始狀態／服務顯示 | | |
| 4-2 不跨日 | | |
