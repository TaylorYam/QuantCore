# Panel 從 Tkinter 換成 PySide6：新舊並行、切換前新 Panel 對實盤只看不下單

Panel（約 5,000 行 Tk，12 個模組）換成 PySide6，理由四個一起成立：外觀老舊；Tk 本身的毛病（同行程第二個 Tk root 不可靠導致 GUI 測試 flake #40、輸入法要自己用 Win32 `ImmAssociateContext` 補 #138）；想要可互動圖表（現在是 matplotlib Agg 出圖片）；為之後擴充畫面打底。範圍**只換介面**：Strategy／IBKR／Database 是概念分層，`automation/*.py` 不搬（搬模組 = 切換 live 行為，見部署模型），Database／History 另案。

**並行切換，不一次重寫替換。** Tk 與 Qt 各有事件迴圈，不能在同一行程逐窗替換，所以新 Panel 是獨立程式，和 Tk Panel 並存到功能補齊才切換。並存期間的兩條硬規則：

- **[[值班 (Watch)]] 只由 Tk Panel 跑**，新 Panel 只顯示狀態 — 兩邊都跑 Telegram 會重複警報。切換當天才交接。
- **新 Panel 對實盤只看不下單**，用新的唯讀 clientId 查權益／持倉。原因：clientId 依帳戶×用途固定分配（`account_utils.get_client_id`），手動單認成交靠 clientId 比對（`manual_orders.py`），兩個 Panel 同身分連 IB 會互踢或認錯單。Trading 畫面在切換前接 paper Gateway 驗證。

切換條件（全部達成）：功能補齊（`portfolio_calc` LEGACY 不搬）；Trading 在 paper 完整跑過手動單（含部分成交、撤單）、Sync Pos、交給策略接管；並行 ≥ 1 週且涵蓋週二、週三，兩邊權益／持倉讀數一致；英數欄位經注音實測。切換後值班、系統匣、桌面捷徑歸新 Panel，Tk 保留約 2 週當退路再刪。

## Considered Options

- **一次重寫、一次切換** — 否決：Panel 管下單與值班，沒有退路的大切換風險太高。
- **把值班拆成獨立背景程式、兩個 Panel 都只顯示** — 長期較乾淨，但屬後端改動，超出「只換介面」範圍；可另開 Issue。
- **後端一起重整成 Backend 資料夾** — 否決（本案）：搬動 live 模組需 characterization，和 GUI 綁在一起會讓兩邊都難驗證。
- **圖表用 matplotlib 嵌 Qt** — 未選。選 pyqtgraph 換取流暢互動；代價是 Panel 與 Telegram 報表（仍用 matplotlib）是兩套畫圖程式、長相不同。

## Consequences

- IB 連線那一支稱 [[Broker（券商連線層）]]，不叫 Execution（與 [[執行盤 (Execution Session)]] 撞名）。
- 新 Panel 版面：Dashboard（Gateway、值班狀態、Log）／Portfolio（三帳戶總覽 → 點進單一帳戶）／Trading（手動單、Sync Pos、接管；送單一律保留確認視窗）。版面細節以丟棄式原型（假資料、不接 IB）確認後為準。
