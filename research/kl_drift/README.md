# KL 散度因子漂移研究

探索 KL 散度作為板塊報酬分布漂移偵測因子的可行性，
以及替代 MA200 牛熊濾網的可能性。

## 檔案

| 檔案 | 說明 |
|------|------|
| `kl_divergence.py` | KL 散度核心計算模組 |
| `KL.pdf` | 理論背景參考文獻 |
| `study_kl_backtest.py` | KL 增強回測 — 對比原始策略與 KL 預警版本 |
| `study_kl_replace_ma200.py` | 用 KL 散度完全取代 200MA 牛熊濾網 |
| `study_kl_vs_ma200.py` | KL 散度與 MA200 的比較分析 |
| `study_factor_drift.py` | 板塊因子漂移偵測 |
| `study_return_drift.py` | 報酬分布漂移偵測 |

## 執行方式

這些是探索性研究腳本，不在 CI 的 pytest 收集範圍。
需要 `data/` 目錄的市場資料，從專案根目錄執行：

```bash
python research/kl_drift/study_kl_backtest.py
```
