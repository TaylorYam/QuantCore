# Constituents／交易日完整性與效能量測（#48）

## 執行方式

在獨立 worktree 執行，`--data-root` 指向資料副本；報告由 stdout 輸出。
工具不修改行情、reference、Strategy、Account 配置或 runtime state，也不連網。

```powershell
python -m data.session_coverage --data-root output/session-study/input --start 2015-01-01 --end 2026-09-17 > output/session-study/coverage.json
python -m data.benchmark_pipeline --data-root output/session-study/input --start 2025-01-02 --end 2025-12-31 --repeats 3 > output/session-study/benchmark-final.json
python -m pytest automation/tests/ -v
```

Coverage exit code：0 = 本次檢查無發現；1 = 有缺口或 reference 問題；2 = 無法完成。
Benchmark exit code：0 = 重複結果一致且無 CSV 載入失敗；1 = 有載入失敗或不一致；2 = 無法完成。
Exit 1 仍會輸出完整 JSON，不能當成工具沒有執行。
請明確提供最近已完成的 Reference Close，不要自動使用今天或 Execution Session。

## 檢查語意

- 只對「指定交易日的確切 Constituents」要求個股行情，不要求整個 Universe 每天都有資料。
- 缺少、空白、含非法代碼或重複日期的 snapshot 列為 unknown；不 forward-fill。
- 本地日曆與 SPY 做雙向比對。日曆有、SPY 無的日期單獨分類，不直接認定為個股漏資料。
- 缺列與無效列分開：日期重複、必需價格非有限正數、Volume 非有限或負數、缺欄位均不可算有效 bar。
- 缺列再分為首次觀察價格之前、最後觀察價格之後、內部缺口；觀察邊界不等於上市／下市日。
- `.txt` 後綴依現行 reader 移除；`.`／`-` 檔名只提示候選，絕不自動改名或合併公司歷史。
- 檢查 raw Constituents，尚未套用 Strategy BLACKLIST；不涵蓋進場前暖機資料、已移出但仍持有的部位或所有 ETF。
- Calendar 仍是待驗證的本地 reference。與 SPY 一致不能證明它沒有共同遺漏。
- JSON 保留逐檔 SHA-256、逐標的缺口日期及輸入格式問題。缺檔沒有可記錄的檔案 hash。

## 2026-09-18 實測發現

輸入為 live 行情及兩份 reference 的隔離副本，另做逐檔 byte hash 比對，與 live 相同。
沒有複製機密配置、Account state 或 Excel 帳戶紀錄。

| 指標 | 結果 |
|---|---:|
| 檢查區間 | 2015-01-01～2026-09-17 |
| 本地日曆列出的 sessions | 2,950 |
| 有確切 Constituents snapshot 的 sessions | 2,949 |
| 區間歷史 Constituents 聯集 | 745 個代碼 |
| 找不到同名 CSV 的代碼 | 121 |
| Raw reference 要求的 ticker-session 配對 | 1,488,571 |
| 有效 bars | 1,351,268 |
| 缺列（尚未排除日曆錯誤） | 137,303 |
| 缺列中發生在日曆有、SPY 無的日期 | 3,019 |
| 缺列中發生在 SPY 有行情的日期 | 134,284 |
| 當日 Constituents 配對中無效 bars | 0 |

最後 snapshot 是 **2026-09-16**；9/17 為 unknown。週三 gated refresh 的設計本來就可能在週四沒有新 snapshot，
因此這是覆蓋界限，不等於 refresh 工作失敗，更不應直接新增每日通知。

9/16 名單有 **A、BF.B、BRK.B、MRSH** 找不到同名 CSV。
BF-B.csv、BRK-B.csv 存在，是格式不一致候選，還需確認 reader 與 provider 的命名契約。
其餘歷史缺檔也不能用今天的公司代碼自動回填，尤其公司合併、改名、下市及代碼重用。

在「已有檔案」標的中，排除六個 SPY 無行情日期後，仍有 8,681 個缺列候選；
其中包括價格歷史前後邊界，不代表全是內部下載漏失。完整清單在 JSON。
這些發現足以說明資料覆蓋需要修復；尚未做修復前後的回測比較，不能推論對報酬的大小或方向。

## 已查證的日曆錯誤

本地 `us_trading_days.xlsx` 與 dense Constituents 包含以下六個休市日。
這次只記錄，不修改 reference，也不在工具中偷偷覆寫日曆：

| 日期 | 休市原因 | 官方證據 |
|---|---|---|
| 2022-06-20、2023-06-19、2024-06-19 | Juneteenth | [NYSE 2022–2024 calendar](https://ir.theice.com/press/news-details/2021/NYSE-Group-Announces-2022-2023-and-2024-Holiday-and-Early-Closings-Calendar/default.aspx) |
| 2025-06-19、2026-06-19 | Juneteenth | [NYSE 2025–2027 calendar](https://ir.theice.com/press/news-details/2024/NYSE-Group-Announces-2025-2026-and-2027-Holiday-and-Early-Closings-Calendar/default.aspx) |
| 2025-01-09 | Carter national day of mourning | [NYSE closure announcement](https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx) |

## 效能量測方法與界限

最終三輪量測，Windows 11、Python 3.13.2、pandas 2.2.3：

| 階段 | 中位數（秒） |
|---|---:|
| Constituents Excel | 13.080 |
| 個股 CSV loader | 3.107 |
| Preload 其餘工作（含 EMA） | 0.450 |
| SPY／SSO CSV | 0.013 |
| Backtest setup | 0.001 |
| Backtest run | 13.748 |
| 各輪 stage 總時間的中位數 | 30.424 |

三輪總時間為 30.712、30.188、30.424 秒。表內各階段中位數不能要求恰好加總成總時間中位數。
Run 中指標計算內含時間中位數為 13.659 秒。
本次 CSV loader 约佔整體一成；Excel 載入和指標計算才是主要耗時候選。
這不足以推論改 SQL 的加速比，需要另做同輸入、同輸出的 A/B 比較。

量測現行 SP500 原碼，區間為 2025-01-02～2025-12-31，共 250 個 SPY sessions。
每次建立新 SelectionEngine，分開記錄 Excel、個股 preload、benchmark CSV、backtest setup、run。
`stock_csv_seconds` 包含 loader 的存在性檢查、讀檔、日期整理，不是純磁碟 I/O。
`preload_other_seconds` 是 preload 總時間扣掉 loader，包含 ticker 枚舉、EMA 準備與 cache 寫入。
`metrics_seconds_within_run` 是 run 的內含時間，不能再加一次。
`csv_calls_during_run` 也包含不存在檔案的重試，不代表每次都有磁碟 CSV 解析。

三次重複均為新的應用程式 cache；OS 檔案 cache 未清空，而且計時前會先 hash 輸入。
因此結果不是 cold disk benchmark。匯入、hash、結果序列化不計入 stage time。
工具用 temporary in-process path bindings 指向副本，離開時還原；不改任何配置檔。
輸入 hash 在量測前後必須相同。輸出同時記錄 Python/pandas、Git commit 與實際程式檔 hash。

成功載入 510／522 個候選標的；12 個檔案讀取失敗，因此本次 benchmark exit code 為 1。
每輪在 run 內還有 346 次 loader 呼叫，對缺檔的重試值得另案量測，不能先宣稱快取可改善多少。
三輪 equity history／trade 序列 hash 一致，只證明本次重跑一致，不是 OLD/NEW characterization。
沒有輸出策略報酬評估，也沒有 SR／Skew LS 或 SQLite／DuckDB 的速度比較。

## 後續順序

1. 另案修正 reference calendar，制定具版本與來源的更新方式；不要重寫原始歷史副本。
2. 核對目前四個同名缺檔與歷史 121 個代碼，區分檔名格式、改名、公司事件與真正資料缺失。
3. 優先量測 Constituents Excel 的快取／衍生格式方案，再評估行情 CSV 搬遷。
4. 若修改 reader、策略輸入或價格歷史，依專案規則另做 OLD/NEW signal-state characterization 與必要回測。

第 1、2、4 項已由 #50 處理，結果與部署步驟見 [data-coverage-repair.md](data-coverage-repair.md)。

本 PR 只新增離線檢查與量測工具，沒有變更 Strategy 實作，未部署 live。
本機完整測試：484 passed、20 skipped；20 項為 clean worktree 未帶入策略預設路徑歷史資料的測試。
新測試以 synthetic Excel／CSV 驗證 membership、unknown snapshot、日曆差異、無效 bar、輸入不被改寫、
真實 SP500 程式重複量測，以及失敗時還原 in-process path bindings。
