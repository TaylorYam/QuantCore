# Reports

Walk-Forward 驗證報告存放區。

## 命名格式

```
v{版本}_{策略}_{變更}_{驗證方法}_{結果}_{日期}.csv
```

範例：`v01_sp500mom_emaentry_WF_N8_PASS_2026-06-10.csv`

| 欄位 | 說明 |
|------|------|
| 版本 | `v01`, `v02`, ... |
| 策略 | `sp500mom` = SP500 Momentum |
| 變更 | 被驗證的設計變更（如 `emaentry`, `tpblock_kurt60`） |
| 驗證方法 | `WF` = Walk-Forward（Pardo） |
| 結果 | `PASS` / `FAIL`，附 N 值（如 `N8`） |
| 日期 | 驗證日期 |

## 保存條件

- 只保留用於支持 ADR 決策或否決提案的驗證報告
- 驗證方法與參數見各對應的 ADR（`docs/adr/`）
- 重現方式：`robustness/` 目錄下的對應腳本
