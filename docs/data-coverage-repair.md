# Constituents／交易日覆蓋與命名一致性修復（#50）

承接 [#48 的唯讀檢查](data-session-study.md)。本文件記錄：官方日曆修正、代碼命名契約、
最新四個缺檔的處置、121 個歷史缺檔與其餘缺口的分類與對帳、staging 的 OLD/NEW 差異、
characterization 與回測 A/B，以及 live 部署與回復步驟。**live 資料替換是另行核准的部署動作，本 PR 不做。**

## 重跑方式

在獨立 worktree 執行；`OLD` 是 live `data/` 的隔離副本（`reference/`、`stocks/`、`etfs/`）。
只有 `probe` 會連網（yfinance、Wikipedia，唯讀）；其餘步驟離線、可重跑。

```powershell
python -m data.coverage_evidence manifest --data-root OLD > manifest-old.json
python -m data.session_coverage --data-root OLD --start 2015-01-01 --end 2026-09-17 > coverage-old.json
python -m data.trading_calendar check --calendar OLD/reference/us_trading_days.xlsx --spy OLD/etfs/SPY.csv
python -m data.coverage_evidence probe --coverage coverage-old.json --data-root OLD --out evidence
python -m data.coverage_evidence classify --coverage coverage-old.json --probe evidence/probe.json > classification.json
python -m data.coverage_evidence stage --old OLD --new NEW --classification classification.json --probe evidence/probe.json > stage.json
python -m data.session_coverage --data-root NEW --start 2015-01-01 --end 2026-09-17 > coverage-new.json
python -m data.coverage_evidence verify --old-coverage coverage-old.json --new-coverage coverage-new.json --classification classification.json
```

`classify` 以 [`data/coverage_dispositions.json`](../data/coverage_dispositions.json) 的人工覆核處置為準；
沒被覆核的缺口套保守預設（provider 無資料 = 真正缺資料／未解決，其餘 = 尚無法確定），**預設永遠不會標成已修復**。
`stage` 只套用覆核過且證據檢查通過的修復（provider 序列需涵蓋所有缺漏 session、hash 與 probe 紀錄一致），
不覆寫既有檔、不寫 `OLD`。`verify` 逐組確認：已修復的組在 NEW 有有效 bar；未解決／已說明的組仍缺，沒有「意外變好」。

本次（2026-09-18）證據快照：yfinance 1.0、pandas 2.2.3；Wikipedia
[S&P 500 公司清單](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies) revision 1375385207、
[歷史成分股變動](https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500) revision 1370891103，
抓取時間 2026-09-18 07:53 UTC。原始頁面、provider CSV 與逐檔 SHA-256 留在本機 `output/issue-50/`，不 commit。

## 1. OLD 基準

隔離副本 655 個檔案（reference 3、stocks 627、etfs 25）與 live 逐檔 SHA-256 相同。
重跑 coverage 的結果與 #48 報告逐項一致（只差錯誤訊息裡的副本路徑）：
2,950 個日曆 session、745 個歷史代碼、121 個缺檔、137,303 個缺列，其中 3,019 個在日曆有、SPY 無的日期。

## 2. 官方日曆

`automation/market_calendar.py` 新增 2014–2027 的官方 NYSE 全日休市與 1:00 p.m. 提早收盤清單，逐年附
NYSE/ICE 公告來源（另含 2018-12-05、2025-01-09 兩個國喪休市）。live 監控原本用的 2026–2027 休市集合不變（有測試釘住）。

獨立驗證：這份清單推出的 2015-01-02～2026-09-17 交易日，與 SPY 實際有行情的 2,944 天**完全一致**；
現行 `us_trading_days.xlsx`（2014-01-02～2026-12-31）與清單的差異**恰好就是**下列六天，沒有其他錯誤：

| 日期 | 休市原因 | 官方來源 |
|---|---|---|
| 2022-06-20 | Juneteenth（補假） | [NYSE 2022–2024](https://ir.theice.com/press/news-details/2021/NYSE-Group-Announces-2022-2023-and-2024-Holiday-and-Early-Closings-Calendar/default.aspx) |
| 2023-06-19、2024-06-19 | Juneteenth | 同上 |
| 2025-01-09 | 國喪（Jimmy Carter） | [NYSE 公告](https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx) |
| 2025-06-19、2026-06-19 | Juneteenth | [NYSE 2025–2027](https://ir.theice.com/press/news-details/2024/NYSE-Group-Announces-2025-2026-and-2027-Holiday-and-Early-Closings-Calendar/default.aspx) |

修正版日曆由 `python -m data.trading_calendar build` 從官方清單重建：移除上述六天、依官方 2027 行事曆延伸到
2027-12-31（3,520 列，欄位格式與原檔相同）。若模板與官方清單的差異不只是「列了休市日」，build 直接拒絕，
不會默默改動其他日期。半日交易（例：2026-11-27、2026-12-24）保留為交易日。

Constituents 的 dense snapshot 在這六天各有一列（2026-06-19 由 2026-06 的 refresh 寫入，其餘來自 2026-01 的原始建檔），
內容都與前一個交易日相同。修正版刪除這六列；其餘 2,943 列、欄位、工作表順序與 Summary／Counts 工作表逐格相同
（Summary／Counts 是原始建檔留下的統計表，沒有程式讀取，照原樣保留）。

### 讓之後的 refresh 不再產生同類錯誤

`data/refresh_constituents.py` 新增兩道 hard gate（halt + Telegram，同既有 gate）：

- **G0**：今天超出日曆涵蓋範圍 → halt。舊行為會把 today 定格在日曆最後一天，之後每週都回報「Already up to date」，
  Constituents 會默默停更（現行日曆只到 2026-12-31，2027-01-06 起就會發生）。
- **G7**：重建區間 (last_date, today] 的日曆與官方清單不一致（列了休市日或漏了交易日）→ halt。
  只檢查本次要寫的區間，歷史錯誤不會擋住例行 refresh。

用 staging 的真實日曆離線模擬（不連網、不寫檔）：

| 情境 | OLD 日曆 | 修正版日曆 |
|---|---|---|
| 重演 2026-06-24 refresh | G7 halt（2026-06-19 Juneteenth） | 通過，不寫 06-19 |
| 2026 感恩節週（11-18→12-02） | 通過；11-26 不寫、11-27 半日照寫 | 同左 |
| 2027-01-06 | G0 halt（日曆止於 2026-12-31） | 通過 |
| 2027 Juneteenth 週 | — | 通過，不寫 06-18 |
| 2028-01-05 | — | G0 halt（需再延伸日曆與官方清單） |

因此程式可以先合併：現行 live 日曆在 2026 年剩餘的區間都與官方一致，G7 不會誤擋；
若修正版日曆沒有在 2027 年前部署，G0 會在 2027-01-06 明確告警，而不是默默停更。
**無法事先偵測**：尚未寫進官方清單的臨時休市，若日曆也沒改，refresh 仍會寫出那天的 snapshot；
事後可由 `session_coverage` 的 SPY 交叉比對抓到。

## 3. 代碼命名契約

| 名稱 | 寫法 | 例 |
|---|---|---|
| Constituents symbol | S&P／Wikipedia，類股以 `.` 分隔 | `BRK.B` |
| Provider symbol | Yahoo，類股以 `-` 分隔；`update_data` 以檔名當下載代碼 | `BRK-B` |
| 本地檔名 | `{provider symbol}.csv` | `BRK-B.csv` |

`strategies/sp500_momentum/utils.provider_symbol()` 只做 `.`→`-` 格式轉換。正式 reader
（`SelectionEngine._get_ticker_data`）、refresh 的新代碼下載（G4 與 staging 檔名）、coverage 稽核都走同一個函式。
策略內部、持倉與 Constituents 仍使用 Constituents symbol。改名、合併、代碼重用**不**在契約內，
不會自動把某公司的歷史接到另一個代碼上。

**Broker 代碼對應**由 `automation/symbol_contract.py` 集中處理：`BRK.B`／`BF.B`
在 IB 合約中分別使用 `BRK B`／`BF B`，並指定 `NYSE` primary exchange；策略、持倉、
Universe 與報表仍使用 Constituents symbol。`config.CLASS_SHARES_TRADABLE = True`，
因此類股可以進入選股與下單流程；詳細查證紀錄見
[`broker-symbol-contract.md`](broker-symbol-contract.md)。

## 4. 最新四個缺檔（2026-09-16 snapshot）

| 代碼 | 分類 | 處置 | 證據 |
|---|---|---|---|
| BRK.B | 格式差異 | 已修復：契約讀 `BRK-B.csv`，IB 對應 `BRK B`（#52） | provider BRK-B = Berkshire Hathaway Inc.；本地檔 3,196 個收盤與 provider 完全一致 |
| BF.B | 格式差異 | 已修復：契約讀 `BF-B.csv`，IB 對應 `BF B`（#52） | provider BF-B = Brown-Forman Corporation；收盤相對差 ≤ 1.4e-4 |
| A | 真正缺資料 | 已修復：補抓 provider `A`（2014-01-02 起） | provider A = Agilent Technologies, Inc.（1999 上市）；Wikipedia 現任成分股 |
| MRSH | 已查證改名 | 已修復：補抓 provider `MRSH` | [Marsh McLennan 公告](https://www.corporate.marsh.com/news-events/2025/december/marsh-mclennan-to-change-nyse-symbol-to-mrsh.html)：2026-01-14 起 MMC→MRSH，法人與 CUSIP 不變；provider MRSH 與本地 `MMC.csv` 在 3,067 個重疊 session 收盤完全一致（最大相對差 6e-8） |

Constituents 從 2015 年起就以 `MRSH` 記錄 Marsh McLennan（原始建檔採用當時的新代碼），而行情一直存在 `MMC.csv`，
所以 Marsh 與 Agilent 之前都不在 live 選股範圍內。`MMC.csv` 不再被任何 Constituents 列引用，保留原樣、不合併檔案。

## 5. 121 個歷史缺檔與其餘缺口：分類與對帳

逐組清單（代碼 × 缺口類型，含證據與後續處置）在 `classification.json`；下表是對帳結果：

| 分類 | 狀態 | 缺列 | 組數 | 說明 |
|---|---|---:|---:|---|
| 日曆錯誤（reference_calendar） | 已修復 | 3,019 | 571 | 六個官方休市日，修正後不再是必要 session |
| 格式差異 | 已修復 | 5,886 | 2 | BRK.B、BF.B |
| 真正缺資料 | 已修復 | 12,895 | 6 | A、AAP、BBWI、EMN、KMX、MBC：同代碼、provider 公司名稱與 S&P 變動紀錄一致，補抓完整序列 |
| 已查證改名／公司事件 | 已修復 | 2,943 | 1 | MRSH |
| 真正缺資料 | 未解決 | 87,235 | 105 | 95 個已下市代碼 provider 已無行情；另有 SCG、VLTO 的起始缺漏、FISV 2025-11-12（provider 也缺）、HOT／COL 等內部缺口 |
| 已查證改名／公司事件 | 未解決 | 24,484 | 32 | 17 個缺檔（改名後資料在別的代碼、跨公司接續或代碼被重用）＋ 15 個既有檔的起始缺口（本地檔其實是後來重用代碼的另一家公司） |
| 上市前／下市後 | 未解決 | 834 | 1 | DAY 2015-01-02～2018-04-25：Ceridian 2018-04-26 才上市、2021-09-20 才以 CDAY 加入指數，屬歷史成分股檔的錯誤，另案修正 |
| 上市前／下市後 | 已說明 | 7 | 4 | TWX、AET、HOLX、DAY：官方收購完成、停止交易後，指數生效日前的 1～3 個 session，不可能有 bar |
| **合計** | | **137,303** | 722 | 與原始報告逐列對帳（已修復 24,743／未解決 112,553／已說明 7） |

121 個缺檔：**已修復 9**（BRK.B、BF.B、A、MRSH、AAP、BBWI、EMN、KMX、MBC）、**未解決 112**，未解決的每一檔都有原因與後續處置：

- **provider 沒有名單期間行情的已下市代碼（95）**：ABMD、AGN、ALTR、ALXN、ANSS、ARG、ATVI、AVP、BCR、BHI、BRCM、BXLT、CELG、CERN、CFN、CHK、CMCSK、COV、CPGX、CSC、CTLT、CTXS、CVC、CXO、DFS、DISH、DNB、DNR、DO、DRE、DTV、ENDP、ESV、ETFC、FDO、FL、FLIR、FRC、FTR、GAS、GGP、GMCR、HCBK、HES、HSP、IPG、JNPR、JOY、JWN、KRFT、KSU、LLL、LLTC、LM、LO、LVLT、MJN、MNK、MON、MRO、MXIM、NBL、NLSN、PBCT、PCP、PDCO、PETM、PLL、PXD、QEP、RAI、RHT、RTN、SBNY、SIAL、SIVB、SNI、SRCL、STJ、SWN、SWY、TIF、TSS、TWC、TWTR、VAR、VIAB、WBA、WCG、WFM、WIN、XEC、XL、XLNX、YHOO。
  需要另一個涵蓋已下市證券的資料源；在那之前回測對這些名單期間維持 unknown（存活者偏差照舊，不填補）。
- **改名後資料在其他代碼（跨代碼別名候選）**：GPS→GAP、FBHS→FBIN、ADS→BFH、DPS→KDP、WYN→TNL、ARNC→HWM、DISCA→WBD、ACE→CB。
  都有 provider／CIK 證據指向同一法人，但對應需要「有日期界限的跨代碼別名」，超出只做格式轉換的契約，另案設計。
- **公司事件、不可直接對應**：DXC（provider 序列 2017-04-03 前是 CSC 的歷史）、AA（provider 把 Alcoa Inc. 的舊歷史掛在分拆出來的 Alcoa Corporation 下）、
  DWDP、HFC、TYC（provider 序列含前身公司歷史）、DISCK（不同股別）、BMS（併入不同註冊人 Amcor）。
- **代碼被重用**：APC、SPLS（provider 現在是別家公司／ETF）；既有檔的起始缺口 ADT、CA、CAM、CCE、DOW、EMC、INFO、NE、PCL、POM、SE、SNDK、STI、TE、TEG
  —— 這些本地檔是後來使用同代碼的另一家公司，缺的是原成員的資料，不能用現有檔補。

改名與下市都只採用有官方來源（公司公告、NYSE/SEC、Wikipedia 引用的 S&P 公告）或 provider 可重現證據的結論；
沒有證據的不推論。完整證據（provider 名稱、首個交易日、本地檔範圍與 hash、Wikipedia 變動列）逐組在 `classification.json`。

## 6. Staging 結果（OLD → NEW）

NEW 與 OLD 只差 9 個檔（其餘 646 個 byte 相同）：

| 檔案 | OLD SHA-256 | NEW SHA-256 |
|---|---|---|
| `reference/sp500_constituents_daily.xlsx` | 5f014161df1891255d29cb7627b35a8343e401a76fc69e95cc331011a9cecdd7 | 0ed45279c69500bcc47fc9f5cf7cea1159f2afbe896200c444cf3fbdd3ca5a7e |
| `reference/us_trading_days.xlsx` | 03cb9ea8a45c41c6c32147297ad6223aa14e5a9568c84002279f961fe45fd826 | e860c623e353980241d242684723e9e43eabdea7dc49c8c6d444f7eb0f1997e6 |
| `stocks/A.csv` | （新增） | 31510c604bd85ce95504d20d683786e6b5240d512d80f5d4156d28eac36e1124 |
| `stocks/AAP.csv` | （新增） | 17b095effc9fcc03adf453179d05fcb31dc5bc6b7699c9c9e93f004b7d21957a |
| `stocks/BBWI.csv` | （新增） | b13bb82a5ae169dec830103ef521ad49479dead081248450bde0c095f2f44c12 |
| `stocks/EMN.csv` | （新增） | 16240d8ccfc067488011c93aa867f573705c0a528ba54204b071bd6b10a5fa05 |
| `stocks/KMX.csv` | （新增） | cddcdb006fd58c392eec61be4031deb59bf02edb66ba5c2632ad7aab8b305d1b |
| `stocks/MBC.csv` | （新增） | c4051a82a67eb98fbbbda090681dff01785f06b71d98cc7a7bcc02e20de4b745 |
| `stocks/MRSH.csv` | （新增） | 85b9c99ad7707155f30e8d77a326c7cc77cf12fb46e3cdef52552d629cbe09e6 |

新增的 7 個 CSV 是 provider 原樣序列（2014-01-02～2026-09-17，MBC 自 2022-12-09），欄位與精度同 refresh 為新代碼建檔的格式；
Close／Adj Close 語意與其他 refresh 建檔相同（Adj Close 以抓取當日為基準），沒有另外轉換。

NEW 重跑 coverage：

| 指標 | OLD | NEW |
|---|---:|---:|
| 日曆 session（2015-01-01～2026-09-17） | 2,950 | 2,944 |
| 必要 bar | 1,488,571 | 1,485,552 |
| 有效 bar | 1,351,268 | 1,372,992 |
| 缺列 | 137,303 | 112,560 |
| 其中日曆有、SPY 無 | 3,019 | 0 |
| 缺檔 | 121 | 112 |
| 2026-09-16 名單缺檔 | A、BF.B、BRK.B、MRSH | 無 |
| 日曆 vs SPY／官方清單 | 6 天不一致 | 一致 |

`verify` 逐組對帳 0 個問題：NEW 缺列 112,560 = 未解決 112,553 + 已說明 7。
Universe（745 個歷史代碼的聯集）前後相同；2026-09-17 仍是 unknown（週三 refresh 的覆蓋界限，照 #48 語意不 forward-fill）。

## 7. Characterization 與回測 A/B

### Characterization（live 訊號，3 Accounts × 5 情境）

harness 與 [characterization 規範](../AGENTS.md#5-測試要求) 相同：`signals.get_sp500_weights` 對 Rosa／Taylor／Yvonne ×
（空倉、現有持股 VEEV/CRL/PANW/IQV、部分、歷史股 AAL/AVB、超額）共 15 組，固定 as_of 2026-09-18（signal date 2026-09-15），
每組呼叫前從 live 狀態檔副本還原 `sp500_state_*.json`，記錄權重、signal date 與呼叫後狀態檔全文；另含 3 個 preview 工具的 SR／SP500 權重。
路徑以 in-process binding 指向資料副本；只讀取 live 狀態檔的副本，live 資料、狀態檔與設定檔都沒有被寫入。

| 比較 | 結果 |
|---|---|
| OLD 程式 × OLD 資料（基準；S2 權重與 2026-09-16 live log 一致） | — |
| NEW 程式 × OLD 資料 | **byte-identical**（reader 重構＋命名契約在現行資料上不改變 live 輸出） |
| NEW 程式 × NEW 資料 | **byte-identical**（A、MRSH 等在目前的 signal date 不進入前 20 名，也不擠掉持股） |

### 回測 A/B（完整 SP500，2018-07-02～2026-09-17）

| | A：OLD 程式 × OLD 資料 | C：NEW 程式 × OLD 資料 | B：NEW 程式 × NEW 資料 |
|---|---:|---:|---:|
| 年化報酬 | 37.87% | 37.87% | 38.74% |
| Sharpe | 1.244 | 1.244 | 1.274 |
| 最大回撤 | −23.97% | −23.97% | −23.96% |
| 交易筆數 | 714 | 714 | 704 |
| 載入代碼數 | 589 | 591 | 598 |

- **C = A（history／trades hash 相同）**：類股 BRK.B、BF.B 讀得到行情後，無論是否排除選股，2018–2026 都沒有改變任何一筆交易
  （先在不排除的版本驗證過一次，最終版加上排除後再跑一次，結果相同）。
- **B vs A** 的差異只來自三段補回的歷史成員：AAP（2018-08～10）、KMX（2019-06～08）、BBWI（2021-04～06）；
  A、MRSH、EMN、MBC、BRK.B、BF.B 在整段回測都沒有被買進。逐年差異：2018 +5.0pp、2019 +2.4pp、2021 −2.8pp，其餘年度 ≈ 0。
- 這是**輸入更正**的影響：回測少了原本漏掉的成分股，方向依年份而定，不代表策略變好。未改任何 Strategy 參數或設計，不需要 WFO。
- 以上數字都用最終版程式重跑確認。仍有 112,553 個缺列未解決（多為已下市公司），回測的存活者偏差依舊存在，不應把 B 當成「修正後的真實績效」。

## 8. 部署（另行核准）與回復

程式（本 PR）與資料（NEW 的 9 個檔）可以分開部署，兩種順序都安全：舊程式讀新資料只多看到 A、MRSH 與歷史補檔；
新程式讀舊資料的 live 輸出與現在 byte-identical，G0／G7 在 2026 年內不會誤擋（見第 2、7 節）。

資料部署（需另行核准，避開 04:30–05:00、16:00、21:00–21:35 的排程）：

1. 確認 live 仍是 OLD：`data/reference/us_trading_days.xlsx`、`sp500_constituents_daily.xlsx` 的 SHA-256 與上表 OLD 相同，
   且 7 個新 CSV 在 live 不存在。若週三 refresh 已改寫 Constituents，先用新的 live 副本重跑 `stage`，不要覆蓋新列。
2. 備份兩個 reference 檔為 `*.bak_<時間>_issue50.xlsx`（同 refresh 備份慣例）。
3. 把 NEW 的 2 個 reference 檔與 7 個 CSV 複製進 live `data/`；逐檔比對 SHA-256 與上表 NEW 相同。
4. 唯讀驗證：`python -m data.trading_calendar check --calendar data/reference/us_trading_days.xlsx --spy data/etfs/SPY.csv` 應為 `ok: true`。

回復：用步驟 2 的備份蓋回兩個 reference 檔，刪除 7 個新增 CSV（其他檔案沒有被改）。
新 CSV 部署後會被每日 04:30 更新續寫，回復時一併刪除即可；若回復前週三 refresh 已寫入新列，回復後重跑一次 refresh。

## 9. 範圍外發現（已另開 Issue）

以下在檢查中查證，但會改變 live 訊號、回測 Universe 或下單流程，超出本 Issue，已各自開 Issue（證據另見 `coverage_dispositions.json` 的 `findings`）：

1. **分割後本地 CSV 未回溯調整（影響 live，[#53](https://github.com/TaylorYam/QuantCore/issues/53)）**：`update_data` 只 append，分割日出現假跳動——APH 2026-09-03（1 拆 2，−48.7%）、
   KLAC 2026-06-12（1 拆 10）、CRWD 2026-07-02（1 拆 4）、CVNA 2026-05-08（1 拆 5）、MNST 2026-08-11（1 拆 2）、
   BKNG 2026-04-06（1 拆 25；Adj Close 另在 03-24 斷層）、DD 2026-06-24（1:3 反分割，+195%）。
   跳空過濾（> 20%）會把這些股票排除約 90 個交易日，持有中則會被 rotation 賣出；回測用的是完整重抓的資料，看不到這個落差。
2. **歷史成分股檔的改名假成員（已另案修正，影響回測，[#54](https://github.com/TaylorYam/QuantCore/issues/54)，見 [`constituents-history-repair.md`](constituents-history-repair.md)）**：「加入後才改代碼」的公司被寫成從 2015-01-02 起就是成員——
   DAY（2021-09-20 以 CDAY 加入）、CPAY（2018-06-20 以 FLT）、EG（2017-06-19 以 RE）、WTW（2016-01-05 以 WLTW）、
   IQV（2017-08-29 以 Q）、SW（2024-07-08）；加入前共有 5,661 個有行情的 session 可被回測選到。
   先移除後再加入的（EQT、PCG、FSLR）處理正確。
3. **身分錯誤的既有檔（影響回測，[#55](https://github.com/TaylorYam/QuantCore/issues/55)）**：`COL.csv` 不是 Rockwell Collins（收盤 0.01～0.85、成交量數百股），COL 於 2015–2018 是成員；
   `NFX.csv`、`AVB.csv` 在代碼被重用／併購後，每日更新接上了別的證券的 bar。
4. **下市後的零成交量平盤 bar**（併入 #55）：例如 TWX 2018-06-15、AET 2018-11-29，稽核目前算作有效 bar。
5. **跨代碼別名**（第 5 節的 8 個候選）與**已下市證券資料源**：[#56](https://github.com/TaylorYam/QuantCore/issues/56)。
6. **類股的 broker 代碼對應**（已完成，見 [`broker-symbol-contract.md`](broker-symbol-contract.md)）：[#52](https://github.com/TaylorYam/QuantCore/issues/52)。
7. **Panel 刷新排程的半日交易日期**：`automation/refresh_schedule.py` 列了官方沒有的 2026-07-02、2027-07-02、2027-12-23
   提早收盤，[#57](https://github.com/TaylorYam/QuantCore/issues/57)。
