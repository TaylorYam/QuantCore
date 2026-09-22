# 分割後本地行情未回溯調整：每日更新防護與既有檔修復（#53）

每日更新原本只追加新 K 棒；provider 事後調整歷史（分割、反分割、以「分割」記錄的配發）時，
本地舊列停在舊尺度，分割日出現假跳空——跳空過濾（> 20%）把該股排除約 90 個交易日、持有中會被
rotation 賣掉。[#55](https://github.com/TaylorYam/QuantCore/issues/55) 的身分守門擋的是「已覆核的代碼
不准再追加」，本 Issue 補的是「**在追加當下**發現 provider 調整或換了證券」。本文件記錄新的更新規則、
判定門檻的資料依據、全檔掃描結果、staging 的 OLD/NEW、characterization、log 重播、回測 A/B，
以及 live 部署與回復步驟。**live 資料替換是另行核准的動作；使用者於 2026-09-22 核准後已執行，紀錄見第 10 節。**

## 重跑方式

在獨立 worktree 執行；`OLD` 是 live `data/` 的隔離副本（`stocks/`、`etfs/`、`reference/`）。
只有 `scan` 會連網（yfinance，唯讀），其餘離線、可重跑；只有 `apply` 會寫 live。

```powershell
python -m data.coverage_evidence manifest --data-root OLD > manifest-old.json
python -m data.price_continuity scan --data-root OLD --out evidence --end 2026-09-22
python -m data.price_continuity classify --data-root OLD --scan evidence          # 由存檔重跑分類
python -m data.price_continuity stage --old OLD --new stage\new --scan evidence   # 寫 stage\stage-manifest.json
python -m data.price_continuity apply --live C:\QuantCore-master\data --stage-manifest stage\stage-manifest.json --dry-run
```

`scan` 對每個個股檔抓 provider 從該檔第一天起的完整歷史（含分割紀錄），逐檔判定並把 provider CSV 與
SHA-256 存在 `evidence/`；`stage` 只替換判定為「需重抓」的檔、不新增不刪除、不寫 `OLD`，並逐檔確認
替換後的檔與 provider 一致；`apply` 要求 live 檔的 hash 仍等於 OLD，否則拒絕（每天 04:30 的更新會改寫
這些檔，所以 staging 要在部署前夕重做）。

本次證據：yfinance 1.0、pandas 2.2.3；OLD = live 2026-09-22 14:49 的副本（664 檔＝637 個股 + 25 ETF +
2 reference，資料到 2026-09-21，逐檔 hash 見 `manifest-old.json`）；provider 抓取時間 2026-09-22 06:50 UTC。
證據與中間產物留在本機 `output/issue-53b/`，不 commit。

## 1. 每日更新的新規則

`data/update_data.py` 下載新資料時，一併重抓最後一列往前 30 天（`OVERLAP_DAYS`）已存的 K 棒，
比對後才決定怎麼寫（判定邏輯在 `data/price_continuity.py`）。#55 的身分守門在更前面：已覆核的代碼
直接跳過，不會走到這裡。

| 情況 | 個股 | ETF |
|---|---|---|
| 沒有新 K 棒 | 不動（舊歷史的問題交給 `scan`，不天天報錯） | 同左 |
| 已存 K 棒與 provider 一致 | 只追加新 K 棒（已存列一個 byte 都不改） | 同左 |
| provider 調整過歷史或代碼可能換了證券 | 抓完整歷史核對：同一證券 → 原子替換整檔；核對不過或下載失敗 → **不續寫**、回報 | **不續寫**、回報（見第 9 節） |

「調整過」的觸發條件（任一）：窗口內有 provider 分割紀錄且日期在本地最後一列之後；窗口**最舊兩根** K 棒的
Close 同時偏離 > 1% 且偏離倍數一致；最舊兩根的 Adj Close 同時偏離 > 10%；provider 回傳新 K 棒卻沒有任何
一根與本地重疊。provider 調整歷史時，事件前**每一根**都會乘上同一倍數，所以一定出現在最舊的 K 棒；
單根事後修正只影響個別幾根，不會觸發。回報的錯誤走既有的「資料更新部分失敗」Telegram。

替換寫檔：同目錄暫存檔 → 一次 `os.replace`，失敗時原檔不動；格式與每日追加寫出的 bytes 相同
（`frame_bytes` 讀回 live 檔再寫出，637 個個股檔全部逐 byte 相同）。

## 2. 判定規則與校準資料

`compare_history(本地, provider 完整歷史)`：

1. **涵蓋**：provider 歷史涵蓋範圍內的每一個本地交易日都要有，否則「核對不過」（代碼被重用、provider 截斷歷史）。
   provider 偶爾落後一個 session（2026-09-21 的 HUBB、UA），比 provider 更新的本地 K 棒不參與比對、也原封保留。
2. **尺度跳動**：看 `log(provider Close / 本地 Close)`。同一證券時它是階梯函數，只在分割日跳；
   逐根變化超過 `0.2% + 1.01 分 ÷ 價格`（容忍舊資料兩位小數捨入）的位置，比較前後 5 根的中位數，
   有持續變化才算跳動。每個跳動都要在 ±2 個交易日內對得上 provider 分割紀錄、倍數差 < 1%，否則「核對不過」。
   只有頭尾一根的變化，對不上分割紀錄就當成單根修正。
3. 本地最後一列之後才生效的分割，最新 K 棒的倍數要剛好等於那些分割紀錄。
4. 偏離 > 2% 的零星 K 棒超過 max(3, 0.2%) 根 → 「核對不過」。
5. **Adj Close**：扣掉 Close 的尺度後，本地與 provider 的 Adj Close 倍數跳動 > 10% → 需要替換
   （超大特別配發，或 Adj Close 比分割先跳，例：BKNG 2026-03-24）。要替換時，provider 的 Adj Close/Close
   只准隨時間上升（股息調整的方向），下降 > 1% 就不匯入。

校準資料：

- 最近 10 根重疊 K 棒的 Close 與 provider 的差異（595 個判定一致的檔、5,960 根）：99.9% 在 0.02% 內，最大 0.057%
  （TRIP）——1% 的觸發門檻有兩個數量級的餘裕。冷門的反向 ETF 則常被事後修正（SZK 日量中位數 615 股，6 月以來
  有 5 根被修正 > 1%），所以規則看的是「最舊兩根同倍數」而不是「任一根偏離」。
- 舊系統轉入的歷史只有兩位小數：NVDA 2014 年價格約 0.40，每日有 1–2% 的捨入差（125 根）；AMCR 經 2026-01 反分割 1:5
  放大後約 0.3%。這些不是跳動（不持續），規則 2、4 都不會誤判。
- DHR 2016、GEN 2020、KDP 2018 的超大特別配發，provider 的 Adj Close 有怪異跳動，但這三檔本地與 provider 一致、
  不需替換，規則 5 的 provider 檢查只在要替換時才做。
- 端對端模擬（把 OLD 副本的 2026-09-21 那列去掉，用真實 provider 重跑 `update_stocks`／`update_etfs`）：
  **0 個錯誤**、所有已存列一個 byte 都沒動、596 檔各補上 1 根新 K 棒，APH 被偵測並自動換成完整歷史
  （3197→3198 列）；ETF 全部正常追加。

## 3. 全檔掃描結果（637 個個股檔）

| 判定 | 數量 | 代碼 |
|---|---|---|
| 需重抓 | 13 | 見下表 |
| 核對不過（不重抓） | 7 | AVB、BBBY、EQR、LEG（provider 只剩 2026-07 之後）、CSRA（2026-08 起）、NFX（provider 的 NFX 已是別的證券）、EA（下市私有化後代碼被接上新資料）；多數已在 #55 的守門名單內 |
| provider 無資料 | 22 | 已下市（AET、TWX…）、改名或被收購（BK、HOLX、K、MMC、SEE、TGNA…） |
| 一致 | 595 | |

需重抓的 13 檔（「跳動」＝本地在該日的假跳空倍數）：

| 代碼 | 跳動日 | 倍數 | provider 紀錄 | 說明 |
|---|---|---|---|---|
| APH | 2026-09-03 | 2 | 同日 ×2 | 1 拆 2 |
| KLAC | 2026-06-12 | 10 | 同日 ×10 | |
| CRWD | 2026-07-02 | 4 | 同日 ×4 | |
| CVNA | 2026-05-08 | 5 | 同日 ×5 | |
| MNST | 2026-08-11 | 2 | 同日 ×2 | |
| BKNG | 2026-04-06 | 25 | 同日 ×25 | 另有 Adj Close 在 03-24 先跳 ×25 |
| DD | 2026-06-24 | 1/3 | 同日 ×1/3 | 反分割 |
| BDX | 2026-02-09 | 1.272 | 02-10 ×1.272 | 分拆子公司，provider 以分割記錄、紀錄晚一天 |
| FDX | 2026-06-01 | 1.241 | 同日 ×1.241 | 分拆（FedEx Freight） |
| SPGI | 2026-07-01 | 1.057 | 同日 ×1.057 | 分拆 |
| HON | 2026-06-29 | 0.9535 | 同日 ×0.9535 | **待確認**：倍數 < 1 且不是 1:n；依 provider，事件前價格提高 4.9%，調整後該日 −6.5%，實際成交價只跌 −1.9% |
| POM | 2026-06-22 | 1/18 | 同日 | 代碼重用的低價股（Pepco 已於 2016 下市），非成分股 |
| AIV | — | — | — | 2025–2026 大額特別配發（Adj Close 倍數 1.33、1.44），2020 年後非成分股 |

#50 的掃描只看 > 35% 的跳動，所以漏了 BDX／FDX／SPGI／HON 這類以分割記錄的分拆調整；本工具看的是「本地與 provider
的尺度是否一致」，不靠跳動大小。非整數倍數（BDX、FDX、SPGI、HON）照樣依 provider 替換（與全新下載一致），
但在 scan 報告與每日 log 標出供人工確認；要排除某檔用 `stage --exclude HON`。

## 4. Staging（OLD → NEW）

13 檔替換、0 新增、0 刪除；12 檔列數不變，POM 多 2 列（provider 有本地沒有的兩個 session）。

| 檔案 | OLD SHA-256 | NEW SHA-256 |
|---|---|---|
| `stocks/AIV.csv` | `5802bc87c532` | `d30a3840cbab` |
| `stocks/APH.csv` | `4461c5f1d76c` | `dd9efb556a2f` |
| `stocks/BDX.csv` | `55ad2188223d` | `f740a0325735` |
| `stocks/BKNG.csv` | `16b29afa7702` | `7938bd0e3825` |
| `stocks/CRWD.csv` | `2367154f07b8` | `306a410846f3` |
| `stocks/CVNA.csv` | `9053d7fb8e0a` | `51144dbb7279` |
| `stocks/DD.csv` | `066fd44d8ced` | `f612e4476ddd` |
| `stocks/FDX.csv` | `1d969396de39` | `4088052c3db8` |
| `stocks/HON.csv` | `f6efb29cfbc1` | `2cdafa0b7941` |
| `stocks/KLAC.csv` | `0b8e684fbed3` | `bc5d1500a9ed` |
| `stocks/MNST.csv` | `4af3ab84b94c` | `fa27629b2a9f` |
| `stocks/POM.csv` | `057ffe53f2e2` | `f9cd27fb1f57` |
| `stocks/SPGI.csv` | `bb2b810587ae` | `505b47e1c2e6` |

完整 hash 在 `stage-manifest.json`。扣掉分割倍數後，NEW 與 OLD 的差別只有：

- **Close**：與 OLD 相同（殘差 ≤ 0.05%）；AIV、POM 是低價股的舊資料捨入差（最大 0.14%、1.6%）。
- **Adj Close**：依 2026 以來的股息重新調整，0.35%–1.9%（CRWD、CVNA、MNST 無股息不變）；AIV 因特別配發 ×0.52。
- **Volume**：乘上分割倍數。

## 5. Characterization（live 訊號，3 Accounts × 5 情境）

同一個 harness（state 檔每個情境前還原、as_of 2026-09-22 → signal date 2026-09-15、同時跑 3 個 preview 工具）：

| 比較 | 結果 |
|---|---|
| main（74ac890）程式 × OLD vs 本分支程式 × OLD | 逐 byte 相同（訊號路徑沒改） |
| 本分支 × OLD vs 本分支 × NEW | 15 個情境的權重、signal date、state **全部相同**；3 個 preview 工具相同 |

排名層面：OLD 有 7 檔被跳空過濾剔除（APH 50%、CRWD 75%、CVNA 80%、DD 210%、FDX 21%、KLAC 90%、MNST 49%），
NEW 全部回到排名。09-15（上次調倉的 signal date）CRWD 第 22、BDX 第 28（OLD 第 34，股息調整的差）、其餘在 80 名以後；
最新收盤 09-21（= 09-23 調倉會用的資料）CRWD 第 24，前 20 名兩組完全相同，
目前持股 CRL／VEEV／IQV／PANW 都在前 20 —— 這次修復**不改變目前、也不改變 09-23 的下單**。

## 6. 過去受影響的調倉（log 重播）

重播所有有 rotation 的週三：2026-04-22（牛市確認後第一次 rotation，之前是熊市／等待）到 09-16，共 21 週 × 3 帳戶
（08-26 因 IB Gateway 連線失敗沒有 rotation）。各帳戶取當天 21:00 那次的持股與 signal date，分別用 OLD 與 NEW
算排名與 rotation 決策。

- **驗證**：用 #50 部署前的資料副本＋當時的設定（06-08 前用 1 月凍結的 Constituents、07-11 前沒有噴出封鎖）重播，
  18/21 週三帳戶的前 10 名與 KEEP/SELL/BUY 和 log 逐項相同（另 3 週差在當時的 Constituents 版本）。
  用今天的資料再跑一次只有 12/21 與 log 相同——因為 #50／#63／#56 之後修過歷史成員與別名——但兩次跑出的
  OLD/NEW 差異結論一致。
- **唯一受影響的一次：2026-08-12（三個帳戶都一樣）**。持股 FTNT、PANW、MU、HUM；MU 跌破 EMA50 被標賣出，
  又以第 4 名買回（= 續抱）。資料正確時 CRWD 排第 3（OLD 因 07-02 的一拆四假跳空 75% 被剔除），
  會改買 CRWD、不買回 MU。
- **沒有持股被分割假跳空誤賣**：各週的賣出決策兩組資料完全相同（分割股在分割當時都不在持股內）。

反事實前推（從 08-12 起兩條路徑各自照規則滾動，報酬都用修正後的價格計、週三開盤成交）：資料正確的路徑 08-12 買 CRWD、
08-26 CRWD 跌破 EMA50 換成 CRL，之後與實際路徑完全重合（實際：08-26 排程因 IB 連線失敗，之後補做同一次 rotation，
MU 換 CRL，08-27 的 log 已是 FTNT／CRL／PANW／HUM；前推的 09-02、09-16 持股也與 log 相同）。
08-12 → 08-26 兩週 CRWD 跌得比 MU 多，**資料正確的路徑 SP500 部位少 4.03%**——這次假跳空碰巧避開了一筆虧損。
修正的目的是讓 live 與回測／全新下載的資料一致，不是提高報酬。

## 7. 回測 A/B（完整 SP500，2018-07-02～2026-09-21）

| | main × OLD | 本分支 × OLD | 本分支 × NEW |
|---|---|---|---|
| 年化報酬 | 38.02% | 38.02% | 37.19% |
| Sharpe | 1.245 | 1.245 | 1.217 |
| 最大回撤 | −23.96% | −23.96% | −23.96% |
| 交易筆數 | 717 | 717 | 719 |

- 前兩欄 history／trades hash 相同。
- NEW 的差異 −8.52pp 全在 2026 年（75.41% → 66.89%）；2019–2025 各年差 ≤ 0.003pp（KLAC 價格尺度不同造成的整數股差，
  名目金額相同）。逐筆比對：2026 以前交易完全相同，唯一不同的決策就是 **08-12 買 CRWD 而非 MU**（CRWD 08-26 以 −16.9% 出場），
  之後路徑重合——與第 6 節 live 重播一致。
- 解讀：用未修的本地資料跑的 2026 回測被這次假跳空「幸運地」高估。這是輸入更正，沒有改任何 Strategy 參數或設計，不需要 WFO。

## 8. 股息造成的 Adj Close 落差（不在本次範圍）

本地 Adj Close 自 2026-01 起是逐日追加的，之後的股息沒有回溯調整（中位數 −0.9%、5% 分位 −3.6%）。
把 608 檔全部換成 provider 完整歷史、重播同樣 21 週 × 3 帳戶：與「只修 13 檔」的決策**完全相同**。
對 live 沒有差別，所以每日更新不因一般股息重抓（Adj Close 觸發門檻 10%，季配息遠低於此）。

## 9. ETF：只偵測、不自動重抓

11 檔 sector ETF 與 SPY／SSO／QQQ 在 2025-12 以前的 Close 其實是含息調整價（與 provider 的 Adj Close 只差之後的股息，
例：SPY 2016–2025 各時點都是 provider Adj Close × 1.0053；2025-12-19 起才等於原始收盤），之後逐日追加的是原始收盤；
反向 ETF 則一直是原始收盤。成因未查（`migrate_data.py` 指定的是 `auto_adjust=False`）。整檔換成 provider 歷史會把
SR／Skew LS／SPY MA200 讀的 Close 換成另一種語意，屬於另一個問題，所以 ETF 偵測到調整時停止續寫並回報，交由人工處理。
目前 ETF 最舊兩根都與 provider 一致，部署後不會觸發（端對端模擬已確認）。

已知的 ETF 既有問題（未修）：6 檔反向 ETF（REW、RXD、SDP、SIJ、SMN、SZK）2026-05-13 反分割 1:2，本地在該日跳 +96%，
provider 的分割紀錄卻記在 05-28；影響 Skew LS 回測損益（live 訊號只用 sector ETF，下單用 IB 即時價）。

## 10. 部署（另行核准）與回復

程式與資料可以分開部署：

- **程式**（合併本 PR 並 pull 到 live）：下一次 04:30 起，偵測到 provider 調整的個股會自動換成完整歷史。
  APH 的分割（09-03）只要還落在下載窗最舊兩根之後（最後一列 ≤ 10-01，也就是 10-02 04:30 以前的更新）就會被偵測，
  程式在那之前上線就會在第一次更新時自動修好；其餘 12 檔的跳動已在窗外，要靠下面的資料修復。
- **資料**（避開 04:30–05:00、16:00、21:00–21:35 的排程，週三另避開 04:50 的 refresh）：
  1. 把 live `data/stocks/`、`data/etfs/`、`data/reference/` 複製成新的 OLD，跑 `manifest`、`scan`、`stage`
     （若程式已上線，APH 會變成一致、只剩 12 檔）；確認 `rescaled` 名單與第 3 節相同或只少了已自動修好的檔。
  2. `apply --dry-run`：必須沒有 problems（live 檔 hash 仍等於 OLD）。
  3. `apply`：先把每個要換的 live 檔備份到 `data/backups/issue53_<時間>/stocks/`（已加入 `.gitignore`），再逐檔原子替換，
     逐檔比對 NEW hash；紀錄寫在備份資料夾的 `apply-record.json`。
### 部署紀錄（2026-09-22 15:09，使用者核准）

13 檔全部換成第 4 節的 NEW（HON 依使用者決定跟 provider 走）。前置檢查：live 逐檔 hash 仍等於 OLD；
備份 `data/backups/issue53_20260922_150944/stocks/`（13 檔，附 `apply-record.json`）；逐檔原子替換後 hash 全等於 NEW。

部署後驗證：

- 13 檔對 provider 重新判定全部 `consistent`。
- `python -m data.health --data-root data --expected-session 2026-09-21`：無整體問題；只有既有的 CSRA（代碼重用檔，#55 覆核名單）
  與 VIX 各一個欄位異常，與本次無關。
- 用 live 資料重跑 characterization：15 個情境、3 個 preview 工具、前 30 名排名與 staging 的 NEW **完全相同**，
  三帳戶權重與部署前一致（CRL 27.0%／IQV 33.4%／VEEV 22.6%／PANW 17.0%，signal date 2026-09-15）。

程式尚未合併時，舊版每日更新仍會正常追加這些檔（修好後的最後一列與 provider 同尺度）。

- **回復**：把備份資料夾裡的檔複製回 `data/stocks/`。若之後已跑過每日更新，被回復的檔會少掉之後追加的 K 棒，
  下一次 04:30 會自動補回（下載窗從最後一列往前 30 天開始）。注意：程式已上線時，分割仍在 30 天窗內的檔
  （目前只有 APH，到 10-02 04:30 為止）回復後會在下一次更新再被自動替換。

## 11. 範圍外發現

1. **sector ETF／SPY 的 Close 語意在 2025-12 前後不同**（第 9 節）：SR 動能、Skew LS 偏態、SPY MA200 讀的 Close
   前段含息、後段不含；反向 ETF 05-13 的反分割跳動也要在決定語意後一起修。
2. **代碼身分錯誤的既有檔仍在被追加**：BBBY、CSRA 目前的 K 棒來自重用代碼的新證券，重疊比對一致所以照常追加
   （本規則只擋「在追加當下」發生的身分改變）；scan 會把它們列為「核對不過」，處置屬
   [#55](https://github.com/TaylorYam/QuantCore/issues/55) 的覆核名單。
3. provider 的完整歷史偶爾比每日更新落後一個 session（2026-09-21 的 HUBB、UA）；判定已把這些 K 棒排除在比對外並保留，
   但若當下剛好有分割要修，會延到 provider 補上後的下一次更新才修。
4. 04:30 抓到的最後一根 K 棒，事後 Open／High／Low／Volume 常被 provider 修正，Close 只有 6 檔被修正且都 < 0.1%；
   不影響訊號，未處理。
