# Price-file identity repair（#55）

`data/update_data.py` 原本把 ticker 當成永久身分。代碼被重用、併購完成或下市後，provider 仍可能回傳同一字串，導致新證券的 bar 被 append 到舊證券檔案。本案把已由 `data/coverage_dispositions.json` 查證的邊界寫成可重跑 manifest：[`data/identity_dispositions.json`](../data/identity_dispositions.json)。

## Audit

Audit 只讀取指定 data root。它檢查：

- reviewed member period 與本地 price sessions 是否 overlap；
- price/volume sanity evidence（例如 `COL.csv` 的 0.01–0.85 close 與低典型 volume）；
- provider name / first-trade metadata（用 `--fetch-provider` 線上抓取，或用 `--provider-json` 重播固定 snapshot）；
- reviewed cutoff 後的 bar，以及前後收盤相同、OHLC 平盤且 volume=0 的 bar。

```powershell
py data/identity_audit.py audit --data-root data
py data/identity_audit.py audit --data-root data --fetch-provider
```

`--fetch-provider` 是明確選擇才會使用網路；CI 與 offline tests 不依賴它。其餘 audit 完全離線。非 #55 邊界的 zero-volume flat rows 會列在報告中，但不會自行推論為下市錯誤。

## Staging / rollback

`repair` 會先複製整個 data root，再只套用 manifest 中的 reviewed 動作：`COL` 移至 `quarantine/stocks/`，`NFX`、`AVB`、`TWX`、`AET` 截斷到最後可信 session。來源檔案不會被覆寫，也不會自動部署到 live。

```powershell
py data/identity_audit.py repair `
  --source C:\QuantCore-master\data `
  --out C:\temp\quantcore-identity-staging `
  --report C:\temp\quantcore-identity-staging-report.json
```

確認 report 的 `after.clean=true` 後，仍需獨立完成 backtest A/B、人工 review 與 live replacement approval。rollback 是刪除 staging directory；source data root 保留不變。

## Append guard

Manifest 中 20 個代碼預設為 `append: refuse`，daily updater 不會盲目 append。`update_data.py --stocks` 仍會逐檔記錄預期 skip，且不觸發部分失敗通知。18 個代碼維持無條件拒絕；`DOW` 與 `SNDK` 僅在本地 CSV 最早一列不早於 manifest 記錄的 provider `first_trade` 時恢復更新。日期缺失、無法解析或早於界限時仍 fail-closed。此規則只容許同一新證券接續行情，不補造舊成分股歷史，也不變更其歷史身份處置。

| 類型 | 代碼 | staged action |
|---|---|---|
| 錯誤身分 | COL | quarantine |
| 代碼重用／併購後接續 | NFX, AVB | truncate 到 reviewed cutoff |
| 下市後平盤 zero-volume | TWX, AET | truncate 到最後交易日 |
| 舊成分股歷史未知的代碼重用 | STI, INFO, CA, SE, EMC, TE, CCE, ADT, CAM, POM, PCL, NE, TEG | preserve；無條件拒絕 append |
| 新公司沿用代碼 | DOW, SNDK | preserve 舊成分股歷史；僅允許本地檔從 provider `first_trade` 起始後 append |

`DOW.csv` 只含 Dow Inc. 的行情，從 provider 首個 when-issued session 2019-03-20 開始；SEC 文件記錄 regular-way trading 自 2019-04-02 開始。[來源](https://www.sec.gov/Archives/edgar/data/1751788/000119312519077434/d718799dex991.htm)。`SNDK.csv` 只含 2025 年新 Sandisk 的行情，從 provider 首個 when-issued session 2025-02-13 開始；SEC 文件記錄 regular-way trading 自 2025-02-24 開始。[來源](https://www.sec.gov/Archives/edgar/data/2023554/000119312525019298/d919795d8k.htm)。舊 Dow Chemical 與 2015–2016 SanDisk 的缺失歷史仍 unresolved，沒有 ticker alias。

這份修正只處理檔案身分與 coverage，不替未知的歷史證券補造 price data，也不把今日 provider 的資料反推成舊公司的行情。

## Data A/B

以完整 SP500 pipeline 在 2018-07-02～2026-09-16（現有 Constituents snapshot 的實際 bounds）比較 source data 與 staging data：

| | A：source data | B：staged data |
|---|---:|---:|
| 載入代碼數 | 603 | 602 |
| CSV read failures | 54 | 55（新增 `COL.csv`，因 quarantine） |
| backtest sessions | 2,063 | 2,063 |
| history SHA-256 | `342c42368eaadca7460ac487f80e21e86db44aae4f97896aa177f77aed9a7002` | 相同 |
| trades SHA-256 | `629bcdc920b031856fc6fd2ad165fcf111feba811fde23c7eee61e37c4d7433e` | 相同 |

這是「資料隔離／截斷」的 A/B，不是策略參數或績效優化研究；截至這個回測區間，受影響檔案沒有進入交易輸出。未知下市證券仍維持缺資料，不以其他公司行情填補。
