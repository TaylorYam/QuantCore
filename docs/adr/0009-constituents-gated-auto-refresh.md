---
status: accepted
supersedes-in-part: 0002-sp500-constituents-on-demand-refresh.md
---

# Constituents refresh 改為每週 gated 自動寫入 — supersede ADR-0002 的 on-demand-only

[ADR-0002](0002-sp500-constituents-on-demand-refresh.md) 拒絕每日 auto-fetch 的理由是「Wikipedia vandalism / schema 變動會 **silent** 污染 live 決策」，代價是使用者需主動記得跑。實務證明這個代價被低估：on-demand 已失效兩次 — 2026-01 凍結清單造成 CIEN 誤歸「Other」（被 rotation 與日度風控跳過）、2026-06-01 起又凍了 34 天。**staleness 是已實現、會重演的風險；vandalism 至今是假設性風險**，且腳本現行實作從頭到尾不是 silent：6 道 gates 在寫入前 halt + Telegram（`die()` 會通知）、staging + atomic rename、成功發 diff 報告。

**決定：** 新增 `--auto` gated 模式並排程 `QuantCore_ConstituentsRefresh_Wed`（週三 04:50 Taipei，避開 04:40 DailyReport、距週三 21:00 SP500 調倉約 16 小時人工餘裕）：

1. **全綠才寫**：任一 hard gate（G1/G2/G4/G6）fail → `die()` = 不寫入 + Telegram halt 告警（原行為）。
2. **soft warning 即停手**：G3（gap 內變動 >10 檔 = vandalism 特徵）或 G5（rename 疑似）→ **不寫入** + Telegram 告警 + exit 2，人工跑互動模式 review 後套用。S&P 一年僅 ~20-25 檔變動，週 gap 幾乎總在 0-2 檔，soft warn 屬罕見事件。
3. **寫入必發 diff 報告**（gap 範圍、events 數、±tickers、新 CSV）— 每次變更皆可事後審計，無 silent 路徑。
4. **morning check 的 7 天 stale 警告保留**，角色轉為自動化的 watchdog：排程若默默死掉（機器未開、任務被停、每週都網路失敗），07:00 照樣告警。

## Considered options

- **偵察自動化**（排程 `--dry-run` + diff 報告，寫入仍手動）：否決 — 仍依賴人工動作完成更新，正是已失效兩次的模式；告警疲勞下「看到報告但沒跑」與「忘了跑」結局相同。
- **全自動含 soft warning 也寫入**：否決 — G3 正是 vandalism 的特徵訊號，該是人看的時刻。
- **每日排程**：否決 — 週 gap 本來就小，每日只增加 Wikipedia 依賴頻率，無實質新鮮度收益（成分股變更對選股的影響以週為尺度）。
- **維持 on-demand + 提醒**：否決 — 提醒（2026-07-05 已加）只緩解「忘記」，不消除「知道但拖延」；且提醒與自動化可並存（本決定第 4 點）。

## Consequences

- 例行週三無人值守更新，Telegram 收 diff 報告；異常週收告警並人工介入 — 人的角色從「記得跑」變成「被叫時看」。
- 殘餘風險：**通過全部 gates 的精緻 vandalism** 會被自動寫入 — 與手動跑時人眼掃過 gate 輸出的防護差異極小，且 diff 報告使其可在下次調倉前被發現。若未來 Wikipedia schema 反覆 break 或出現 gate 穿透案例，重評（可能改 iShares IVV 源，ADR-0002 曾拒因依賴成本）。
- ADR-0002 的 gates、staging/atomic 寫入、dense reconstruction 設計**全部不變** — 本 ADR 只讓渡「觸發方式」：on-demand-only → 排程 gated。
- **日曆 gates（2026-09-18 補，#50）**：`us_trading_days.xlsx` 曾把 2022–2026 Juneteenth 與 2025-01-09 國喪休市列為交易日，dense reconstruction 因此寫出休市日 snapshot；日曆又只到 2026-12-31，之後 refresh 會把 today 定格在最後一天、永遠「Already up to date」。新增兩道 hard gate：G0 = 今天超出日曆涵蓋 → halt；G7 = 重建區間 (last_date, today] 的日曆與官方 NYSE registry（`automation/market_calendar.py`，逐年附 NYSE/ICE 公告）不一致 → halt。未排程的臨時休市（國喪等）須先補進 registry。細節見 [data-coverage-repair.md](../data-coverage-repair.md)。
- **Ticker 改名的處理（2026-07-05 補，同日 SATS→ECHO 實案）**：Wikipedia changes 表明文規定 ticker changes 不入表 → 改名永遠不會出現在事件源，G1 對每次改名都會結構性 fail（`--auto` 會照設計告警不寫）。解法 = `--rename OLD=NEW[:date]` **受監督**參數：操作員查證後把改名注入成合成事件，走同一條管線（diff、G4、G5 必 warn、G1 復核），禁與 `--auto` 併用。不是 ADR-0002 拒絕的 sidecar — 無第二資料源，寫入後 snapshot 仍是唯一 truth。全自動改名推斷（CIK/名稱比對）評估過，對年頻 1-2 次的事件過度工程化，reject。
- **晚登錄事件（2026-10-24 補，#249）**：Wikipedia 會在我們寫完名單之後才補登、生效日早於名單最後一天的事件（2026-10：10-07 寫完名單後才補登 10-01 +VYLR、10-06 −CTVA、10-06 +TWLO −WBD），重建只套用生效日 ≥ 名單最後一天的事件，這種事件被永久跳過、之後每週 G1 fail。使用者選 **B 停下並講清楚**：往回 180 天內、生效日到名單起點之間沒有任何一列反映的事件 = 晚登錄 → 不寫入、`die()` 告警，白話列出每筆事件、疑似改名的成對代碼（分開寫）、從哪天重建、對應備份與要跑的那一行指令。重建用受監督的 `--rebuild-from YYYY-MM-DD`：以那天之前的最後一列為起點重建並改寫之後的列，照樣過所有 gate、顯示改寫哪些列的 diff、要人確認，可和 `--rename` 併用，禁與 `--auto` 併用。否決 A（自動回補改寫過去的列，無人值守）與 C（維持 G1 失敗訊息）。
