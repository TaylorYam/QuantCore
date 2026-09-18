# 歷史 Constituents 改名假成員修復（#54）

## 修復規則

修復規則集中在 [`data/constituents_history_repairs.json`](../data/constituents_history_repairs.json)，來源是
[Wikipedia Historical components of the S&P 500](https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500)，revision `1370891103`。
工具只清除「現行代碼在加入生效日前的成分股值」；不把舊代碼倒填、不合併行情檔、不用今天的名單推論歷史。

| 現行代碼 | 指數加入日 | 加入時代碼 | 修正前被誤列至 | 行情 bar 重疊 |
|---|---:|---|---:|---:|
| DAY | 2021-09-20 | CDAY | 2021-09-17 | 856 |
| CPAY | 2018-06-20 | FLT | 2018-06-19 | 872 |
| EG | 2017-06-19 | RE | 2017-06-16 | 619 |
| WTW | 2016-01-05 | WLTW | 2016-01-04 | 253 |
| IQV | 2017-08-29 | Q | 2017-08-28 | 669 |
| SW | 2024-07-08 | SW | 2024-07-05 | 2,392 |
| **合計** | | | | **5,661** |

## 可重跑 staging

先唯讀稽核：

```powershell
python -m data.repair_constituents_history audit `
  --source data/reference/sp500_constituents_daily.xlsx `
  --data-root data
```

產生新檔，不覆寫 source：

```powershell
python -m data.repair_constituents_history repair `
  --source OLD/reference/sp500_constituents_daily.xlsx `
  --out NEW/reference/sp500_constituents_daily.xlsx `
  --data-root OLD `
  --report output/issue-54/repair-report.json
```

本次 live workbook 的 audit 結果為 6,495 個 pre-join membership cells，其中 5,661 個有行情 bar；staging 後 pre-join cells 為 0。原始 workbook SHA-256：
`0ed45279c69500bcc47fc9f5cf7cea1159f2afbe896200c444cf3fbdd3ca5a7e`。

Rollback 是刪除 staged output；工具不會寫 source 或 live `data/reference`。

## SP500 A/B

同一份行情資料、同一個 strategy code、同一時間範圍（2018-07-01～2026-09-16，初始資金 `$1,000,000`）：

| | OLD | NEW | 差異（NEW - OLD） |
|---|---:|---:|---:|
| Final equity | 14,512,482 | 13,703,003 | -809,479 |
| CAGR | 38.65% | 37.68% | -0.97 pp |
| Sharpe | 1.227 | 1.202 | -0.025 |
| Max drawdown | -23.964% | -23.964% | +0.0003 pp |
| Trades | 704 | 705 | +1 |

這是輸入 Universe 修正造成的回測結果差異，不是 Strategy 行為宣稱；未修改 Strategy code，也未做 WFO。

## Live characterization

以 `data/characterize_constituents.py` 執行 3 Accounts × 5 scenarios 共 15 組：

- OLD/NEW 的 signal output hash：15/15 相同。
- OLD/NEW 的 state file hash：15/15 相同。
- Constituents input hash 改變，符合本 issue 的歷史資料修復範圍。
- live snapshot、state 與設定檔均未被寫入；修正版 workbook 需另行核准部署。
