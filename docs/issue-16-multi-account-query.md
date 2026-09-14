# Issue #16 — 多帳戶查詢設計討論

Issue: https://github.com/TaylorYam/QuantCore/issues/16

## 已確認範圍

- 第一版聚焦 Panel 多帳戶並行查詢，改善逐一切換 Account 等待 NAV／持倉的體驗。
- 下單流程維持現狀；本次不加入多帳戶並行送單。
- Panel 開著時，每小時自動更新三個 Account 的查詢資料。
- Panel 啟動立即查詢三個 Account，之後以啟動時間為基準每小時更新。手動 Refresh 同樣更新三個 Account，但不重設每小時計時；切換 Account 只讀取已保存結果，不觸發查詢。
- 核心交付是三個 Account 同一輪並行查詢，按 Account 分別保存結果，供後續 Panel 在同一畫面展示三帳戶使用；本次不做三帳戶畫面改版。
- 各 Account 查詢完成即提供結果，不等待其他 Account；單一 Account 逾時或失敗不阻塞其他 Account。
- 更新失敗保留該 Account 最後一次成功資料，標示更新失敗及最後成功時間；不以零持倉取代。
- 當輪查詢失敗不立即自動重試，等待下一次每小時更新或手動 Refresh；首次查詢失敗且無成功資料時顯示「尚無資料」。
- 手動或定時更新重疊時，正在查詢的 Account 略過重複請求，其他 Account 正常更新；不排隊累積請求。
- MOO 在截止時間前被接受後參與開盤成交，不需要為了開盤成交而同時送單。Panel Sync Pos 現行使用 LMT 並允許盤前盤後，與排程 MOO 路徑不同。

## 程式碼現況

- `automation/gateway_monitor.py` 已為各 Account 建立獨立心跳監測連線。
- `automation/control_panel.py` 的持倉查詢僅讀取選中 Account，使用全域 `_ib_refresh_running` 防止重複查詢。

## 實作方向

- 沿用 `ib_fetcher.fetch_portfolio(ctx)` 的單次查詢生命週期：建立連線、取得結果、結束時清理連線。三帳戶使用各自的 AccountCtx 與工作執行緒／event loop，不跨執行緒共用 IB 物件。
- 查詢協調與每帳戶結果保存獨立於 Tk 畫面；以明確 Account 名稱提供結果與狀態，讓後續三帳戶總覽可直接使用。
- 保存最後成功資料、最後成功時間、查詢中狀態及最近錯誤。資料先保存在 Panel 程序記憶體中，重啟後重新查詢；本次不新增磁碟快取。
- Tk 主執行緒捕捉操作及更新畫面，背景結果依 Account 歸屬保存；晚到結果不得覆蓋其他 Account 的畫面。
- Panel 關閉時取消定時工作，忽略關閉後回傳的畫面更新；查詢資源在 worker 結束時清理。
- 沿用現有關閉語意：縮到 system tray 仍繼續每小時更新；真正退出程序才停止排程。
- 保留現有 Gateway 心跳監測與下單流程。此次只提供現有單帳戶畫面必要的查詢狀態與結果接線，不進行三帳戶版面改版。

## 離線驗收條件

- 用受控 fake 查詢驗證三 Account 都能在任一查詢完成前啟動，不使用真實 IB 或 Telegram。
- 以不同完成順序驗證各 Account 完成即發布自己的結果；單一失敗不阻塞其他 Account。
- 驗證失敗保留最後成功資料與時間；首次失敗維持無資料狀態。
- 驗證同 Account 不重入、不累積請求，其他閒置 Account 仍可刷新。
- 用 fake clock／scheduler 驗證啟動即刷新、每小時刷新、手動刷新不改變週期，以及切換 Account 不觸發查詢。
- 驗證切換 Account 後的晚到結果不會顯示到錯誤 Account，關閉後不更新已銷毀畫面。
- 在隔離 worktree 執行 `pytest automation/tests/ -v`。不接觸 live config、runtime state 或市場資料。

## 測試環境

- 開發 worktree：`C:/QuantCore-worktrees/issue-16-concurrent-ib`，實盤目錄保持原分支與檔案。
- 離線測試使用 fake AccountCtx／API、事件同步與 fake scheduler；無市場資料的既有測試沿用 skip，不複製 live state 或配置。
- 原版 `origin/main`（c29230f）的查詢輸出以 fake API 抓取三帳戶 × 五情境，固定日期後保存於 `automation/tests/fixtures/portfolio_legacy.json`；新版預設查詢路徑逐一比對序列化結果。
- 查詢流程不呼叫策略訊號產生器、不改策略 state；上述 characterization 是查詢輸出基準，不是策略 signal／state 的 characterization。
- 真實驗收工具：`automation/check_portfolio_queries.py`，必須明確提供 `--live-readonly`；只執行一輪查詢，不啟動 Panel／值班／Telegram／交易模組。
- 工具從實盤設定唯讀取得 Account 身分與連線資訊；測試 ctx 的 Allocation 為零、dry_run 為真、Telegram 憑證為空。測試主連線 ID 為 16025/16035/16045，備援報價 ID 為 17025/17035/17045；與現有角色及固定 99 分離。
- 實際查詢以 `readonly=True` 連線，非零 client ID，不接管或綁定既有訂單；此參數是客戶端同步模式，不代表更改 Gateway 權限。安全範圍由工具僅呼叫查詢函式保證。
- 報告寫在開發 worktree 的 gitignored `output/`，只含成功狀態、時間、持倉筆數及測試 ID，不保存帳號、NAV 數值或標的明細。
- 對照實盤 code/config/state/report 檔案雜湊及原有 TCP 連線，確認測試沒有改寫檔案或中斷既有連線。這些檢查不等同證明 Gateway 內部完全沒有負載變化。

## 目前驗證

- 初始離線基準：285 passed、20 skipped（缺少市場資料）。
- 實作後離線測試：312 passed、20 skipped；新增 27 項包含並行、重複刷新、失敗保留資料、排程、畫面歸屬及唯讀連線隔離。
- 2026-09-14 真實唯讀驗收：三帳戶全部成功；開始時間約為啟動後 0.0013–0.0014 秒，Taylor 65.98 秒、Rosa 62.03 秒、Yvonne 65.61 秒完成，三查詢期間重疊。這是一輪並行驗收，沒有執行串行 A/B，因此不宣稱特定加速倍數。
- 測試前後 52 個實盤程式／配置／state／報表檔案 SHA-256 一致；原有 6 條 Gateway TCP 連線全部保留，結束後仍為 6 條，測試連線已退出。實盤 Git working tree 保持乾淨。
- 沒有啟動開發版完整 Panel 或更換實盤程式；Tk 接線用隔離方法測試，尚未做完整 GUI 人工驗收。

## 重現方式

離線：在開發 worktree 執行 `python -m pytest automation/tests/ -v`。

真實唯讀驗收（僅在已獲准使用 API、避開送單時段並確認測試 ID 未使用時）：

```powershell
python automation/check_portfolio_queries.py --live-readonly --source-root C:/QuantCore-master --output output/issue16-readonly.json
```

工具執行一次後退出，不安排重複驗收；失敗情境仍只在 fake API 測試。
