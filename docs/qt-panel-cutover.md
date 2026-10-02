# 新 Panel 切換與回退（#154）

把值班、實盤送單、桌面捷徑從 Tk Panel 交給新 PySide6 Panel 的步驟。決策見
[ADR-0012](adr/0012-panel-pyside6-parallel-cutover.md)，spec 見 #144。切換由使用者執行。

## 開關只有一個

`automation/config.json` 的 `qt_panel_mode`：

| 值 | 新 Panel | Tk Panel |
|----|----------|----------|
| `parallel`（預設，沒寫也是） | 只看；Trading 只接 paper；不值班 | 照舊：值班、實盤送單 |
| `production` | 值班（Rosa poll／TY check／Morning check）＋實盤送單 | 只看：不值班、不能送單，視窗標題寫「只看」 |

兩個 Panel 讀同一個設定，所以不會出現兩邊同時值班或都沒人值班。改設定後要**重開**
兩個 Panel 才會生效（它們只在開啟時讀一次）。

正式模式下新 Panel 改用 Tk 原有的 IB 連線身分（clientId role），Tk 則改用新 Panel
並行期間的唯讀身分，兩邊同時開也不會搶號：

| 用途 | 正式模式的新 Panel | 正式模式的 Tk Panel |
|------|-------------------|---------------------|
| Gateway 心跳／值班 | `ROLE_MONITOR`（Tk 原本的） | `ROLE_PANEL_READONLY`，唯讀 |
| 權益／持倉查詢 | `ROLE_PANEL`（Tk 原本的） | 新 Panel 的查詢號碼（69／79／89） |
| 手動單 | `ROLE_MANUAL_ORDER`（同 Tk，認成交靠它） | 不能送 |
| Sync Pos／接管 | `ROLE_REBALANCE`／`ROLE_TAKEOVER`（同 Tk） | 不能送 |

紀錄與通知也和 Tk 一樣：手動單紀錄 `manual_orders.sqlite3`、手動歸屬
`manual_attribution.sqlite3`、SP500／SkewLS state、`trade_log.xlsx`，以及各帳戶的
Telegram。切換前 Tk 送出、還沒結案的手動單，切換後由新 Panel 接著追蹤與通知。
`qt_panel_paper` 設定在正式模式下用不到，可以留著。

## 切換檢查清單

全部打勾才切換（ADR-0012 的切換條件）。

- [ ] **功能補齊**：新 Panel 的 Dashboard、Portfolio（含權益曲線、策略參數、最近成交）、
      Trading（手動單、Sync Pos、交給策略接管）、Gateway 一鍵啟動與帳密、系統匣都看過，
      沒有缺 Tk 上每天會用的東西（`portfolio_calc` 的 LEGACY 畫面不搬）
- [ ] **paper 完整跑過手動單**：送出、部分成交、撤單（含修改＝先撤再送）、過期
- [ ] **paper 跑過 Sync Pos**：預覽 → 確認 → 送出，暫停原因顯示正確
- [ ] **paper 跑過交給策略接管**：手動單 → 進 Universe 產生暫停 → Sync Pos 看到暫停 →
      接管 → 成交後解除暫停
- [ ] **並行 ≥ 1 週，涵蓋週二、週三**：兩邊三個帳戶的權益與持倉讀數一致
- [ ] **注音實測**：英數欄位（代號、股數、價格）在注音輸入法下打得出半形英數
- [ ] **時間**：在非交易時段切換，並避開
  - 週二／週三台北 20:05 的 TY check
  - 台北 21:00–05:00 的 Rosa poll 窗（每 30 分一次）
  - 也避開 21:00 排程調倉、21:31 成交回報前後

  建議：交易日台北 09:00–19:30，或週末白天。

## 切換步驟

1. 確認沒有正在進行的 Sync Pos 或接管（Tk 視窗都關掉）。未結案的手動單可以留著，
   切換後新 Panel 會接手。
2. Tk Panel：系統匣圖示 → 離開。新 Panel 如果開著也離開（系統匣 → 離開）。
3. 備份 `automation/config.json`，然後只改一個欄位：
   ```json
   "qt_panel_mode": "production"
   ```
   其他欄位（`alloc_*`、`leverage`、`port`、`account` 等）一律不動。
4. 開新 Panel，確認：
   - 標題列顯示「正式模式 · 值班＋實盤送單」
   - Dashboard 值班卡寫「新 Panel 值班中」
   - Output Log 有 `[GW] Rosa poll: …`、`[GW] TY check: next at …`、`[GW] Morning check: next at …`
   - 三個 Gateway 連上（心跳用的是 Tk 原本的號碼，Tk 已關所以不會撞）
   - Trading 上方寫「正式模式：手動單、Sync Pos、接管送到實盤帳戶」
5. 換桌面捷徑：
   ```bash
   python automation/create_shortcut.py --qt
   ```
   會建立（或更新）桌面「QuantCore Qt — Taylor」。再把桌面舊的「QuantCore — Taylor」
   移到別的資料夾（回退期間可能要用；誤開也沒關係，正式模式下 Tk 只看）。
6. （可選）開一次 Tk Panel 確認標題寫「只看」、Output Log 有「值班與實盤送單已交給新
   Panel」，按「手動下單」只會跳說明。確認完可以關掉。
7. 之後第一次值班時點（Rosa poll 窗開始、下一個 TY check、下一個 Morning check）看新
   Panel 的 Output Log 有沒有對應的 `[GW] … → ok`；有異常照舊會發 Telegram。

## 回退步驟（切換後約 2 週內）

新 Panel 出問題、要改回 Tk 值班與下單時：

1. 新 Panel：系統匣 → 離開。
2. `automation/config.json` 的 `qt_panel_mode` 改回 `"parallel"`（或刪掉這一行）。
3. 用舊捷徑或 `automation/QuantCore_Panel.vbs` 開 Tk Panel（捷徑不見了就跑
   `python automation/create_shortcut.py` 重建「QuantCore — Taylor」）。
4. 確認 Tk 的 Output Log 有 `[GW] Rosa poll: …` 等三行排程、標題不再寫「只看」，
   「手動下單」與「Sync Pos」可以正常打開。
5. 新 Panel 之後再開會回到並行模式（只看、Trading 只接 paper），可以繼續並存。

回退不會丟紀錄：兩邊用同一份手動單紀錄、歸屬紀錄、state 與 `trade_log.xlsx`。

## 收尾

切換後約 2 週、確定不再回退，才刪除 Tk Panel（#155）。
