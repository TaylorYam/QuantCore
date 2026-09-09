"""
Sector Rotation MOM — 參數設定
所有可調參數集中在此，其他模組從這裡 import。
"""

from pathlib import Path

# ── 資料路徑 ──────────────────────────────────────────────────
_ROOT    = Path(__file__).resolve().parent.parent.parent
DATA_DIR = _ROOT / "data" / "etfs"

# ── 板塊 ETF 清單 ─────────────────────────────────────────────
SECTOR_TICKERS = [
    "XLK", "XLF", "XLV", "XLE", "XLI",
    "XLY", "XLB", "XLP", "XLU", "XLC", "XLRE",
]
BENCHMARK_TICKER = "SPY"

# ── 動能訊號參數 ──────────────────────────────────────────────
# 多週期加權動能（lookback天數: 權重）
MOM_LOOKBACKS = {
    60: 0.1,
    20: 0.2,
    10: 0.3,
     5: 0.4,
}

# 橫截面 Z-score 門檻：Z < 此值的 ETF 不納入持倉
MOM_ZSCORE_THRESH = 0.4

# ── 再平衡參數 ────────────────────────────────────────────────
REBAL_WEEKDAY = 1       # 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri
REBAL_FREQ    = 1       # 每幾週再平衡一次

# ── 部位管理 ──────────────────────────────────────────────────
WEIGHT_STEP = 0.05      # 持倉四捨五入至 5% 步進

# ── 交易成本 ──────────────────────────────────────────────────
# IB 全成本估算（含點差）0.05% × 1.5 = 0.075%（單向）
TRANSACTION_COST = 0.00075
