# 動量加速度因子研究

探索「動能加速度」（adj_slope 的 N 日變化量）對 SP500 動能策略排名的影響。
在不修改現有程式碼的前提下，獨立計算加速度修正後的排名，並與原始排名做回測比較。

## 檔案

| 檔案 | 說明 |
|------|------|
| `study_acceleration_signal.py` | 加速度信號品質分析（分布、相關性、穩定性） |
| `study_acceleration_backtest.py` | 加速度修正 vs 原始排名回測比較 |
| `study_edge_cases.py` | 合成數據驗證加速度公式邊界情況 |

## 執行方式

這些是探索性研究腳本，不在 CI 的 pytest 收集範圍。
需要 `data/` 目錄的市場資料，從專案根目錄執行：

```bash
python research/momentum_acceleration/study_acceleration_signal.py
```
