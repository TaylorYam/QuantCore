"""
Sector Skew LS — 參數設定
所有可調參數集中在此，其他模組從這裡 import。

參數值與 C:/版塊輪動多空版本/config.py 完全一致（walk-forward 已驗證
Sharpe ~1.3 / MDD ~-8%），只調整 DATA_DIR 指向 QCORE 的 data/etfs/。
"""

from pathlib import Path

# ── 資料路徑（與 strategies/sector_rotation 對齊）─────────────────────────
_ROOT    = Path(__file__).resolve().parent.parent.parent
DATA_DIR = _ROOT / "data" / "etfs"

# ── Universe ──────────────────────────────────────────────────────────────
# 11 SPDR 板塊 ETF
SECTOR_TICKERS = [
    "XLK", "XLF", "XLV", "XLE", "XLI",
    "XLY", "XLB", "XLP", "XLU", "XLC", "XLRE",
]
BENCHMARK_TICKER = "SPY"
VIX_TICKER       = "^VIX"          # yfinance 用 ^VIX；本地存成 VIX.csv
VIX_LOCAL_NAME   = "VIX"           # 本地 CSV 名稱

# ── 反向 ETF 映射 ────────────────────────────────────────────────────────
# sector_ticker -> (inverse_ticker, leverage_abs)
# v4 (2026-05-13): 從 V_trim revert 回 V_full（PDF 原 10 檔）
#   理由：之前 V_trim 的論點「SIJ spread 寬」在 MOO + per-trade commission 模型下不成立。
#   per-trade 真實 cost 分析顯示：
#     - V_full Sharpe 1.06 (per-ticker TC) > V_trim 0.96 > V_all_direct 0.95
#     - V_full TC drag 0.67%/yr (低於 V_trim 的 0.75%) — 2x 槓桿縮小部位、換手較少
#     - 2x inverse ETF 在持續下跌期間（COVID/2022）的每日複利 alpha 是真實 edge
#   詳見 reports/sweep_universe / 與 commission 驗證腳本
INVERSE_ETF = {
    "XLK":  ("REW", 2),   # ProShares UltraShort Technology
    "XLF":  ("SEF", 1),   # ProShares Short Financials
    "XLV":  ("RXD", 2),   # ProShares UltraShort Health Care
    "XLE":  ("ERY", 2),   # Direxion Daily Energy Bear
    "XLI":  ("SIJ", 2),   # ProShares UltraShort Industrials
    "XLY":  ("SCC", 2),   # ProShares UltraShort Consumer Discretionary
    "XLB":  ("SMN", 2),   # ProShares UltraShort Materials
    "XLP":  ("SZK", 2),   # ProShares UltraShort Consumer Staples
    "XLU":  ("SDP", 2),   # ProShares UltraShort Utilities
    "XLRE": ("REK", 1),   # ProShares Short Real Estate
    # XLC: 無對應反向 ETF → 直接放空 1x，DIRECT_SHORT_TICKERS 處理
}
DIRECT_SHORT_TICKERS = ["XLC"]

# 所有需要下載的反向 ETF（給 update_data 用）
INVERSE_TICKERS = [v[0] for v in INVERSE_ETF.values()]

# ── 偏態因子 ──────────────────────────────────────────────────────────────
# window_size_in_days: weight
# v5 (2026-05-13): 從 P9 long-tilt 改為 P10 academic (1M/3M/6M 等權)
#   - 之前選 P9 是基於 uniform 7.5 bps TC + weekly-Thu rebal
#   - 加入 per-trade commission 模型後，DAILY rebal TC drag 只剩 0.4-0.5%/年
#   - 在 daily + V_full + per_trade @ \$1M Tiered 下：
#       P9 long-tilt:  Sharpe 1.235  Calmar 1.075  MDD -10.43%
#       P10 academic:  Sharpe 1.262  Calmar 1.394  MDD -8.23%   ← 全勝
#   - P10 用 1 月 + 3 月 + 6 月 等權偏態，捕捉「短中長期 skew 結構」
#   - daily rebal 讓 inverse ETF 複利 alpha 每日落袋
#   - 接近 PDF 原報告 Sharpe 1.31 / MDD -8.68% 的目標
#   - 詳見 sweep_windows_deeper 和 WFT 驗證
SKEW_WINDOWS = {
    21:  0.33,    # ~1 month
    63:  0.33,    # ~3 months
    126: 0.34,    # ~6 months
}

# ── 多空選股 ──────────────────────────────────────────────────────────────
N_SIDES = 5            # top 5 long, bottom 5 short (共 10 檔，剩 1 檔不持倉)

# ── VIX Regime ────────────────────────────────────────────────────────────
VIX_ZSCORE_WINDOW = 200
# v2: 從 PDF 預設 1.8 改為 2.0
#   - WF (N=6/7/8) 顯示 2.0 在 21 folds 中是 mode pick (9 次，超過 1.8 的 2 次)
#   - 全資料 backtest: Sharpe 1.19 (vs 1.8 的 1.20)、Calmar 1.21 (vs 1.11)、
#     MDD -8.49% (vs -9.30%) → 風險調整後改善
#   - 詳見 reports/wf_N7_PASS_*.csv
VIX_ZSCORE_THRESH = 2.0
ALLOC_HIGHVOL = (0.5, 0.5)    # (long_total, short_total) when VIX z > thresh
ALLOC_LOWVOL  = (0.7, 0.3)    # (long_total, short_total) when VIX z <= thresh

# ── 再平衡 ────────────────────────────────────────────────────────────────
# v6 (2026-05-13): 從 daily 改為 threshold-based (t=0.40)
#   - daily Sharpe 1.26, threshold 0.40 Sharpe 1.31 (跨所有帳戶 size 都贏)
#   - threshold 過濾掉「沒方向的噪音變動」，只在 |Δw|.sum() > 0.40 才換倉
#   - 換手率從 daily 236 events/yr → 84 events/yr，TC drag 0.55% → 0.20%
#   - $10K 帳戶從 daily 0.28 → t=0.70 1.02（注意小帳戶最佳 threshold 是 0.70 而非 0.40）
#   - 詳見後續 WFT 驗證
REBAL_FREQ      = "threshold"   # "daily" | "weekly" | "threshold" | "monthly"
REBAL_THRESHOLD = 0.40          # threshold 模式下的觸發門檻（|Δw|.sum()）
REBAL_WEEKDAY   = 3             # weekly 模式下的星期 (僅 weekly 模式使用，留 hook)

# ── 交易成本 ──────────────────────────────────────────────────────────────
# v5 (2026-05-13): 從「uniform bps」改為「per-trade commission with $min/order」
#   - 之前 7.5 bps blanket 估計嚴重高估真實成本
#   - MOO 訂單沒有 spread cost；只有 IB commission
#   - 真實 $1M 帳戶 IB Pro Tiered TC drag ≈ 0.17%/yr ≈ 0.8 bps 單向
#   - per-trade 模型按每筆部位 size 算 max($min, shares × $/share)，精確反映 $min 對小帳戶的衝擊
#
# COMMISSION_MODEL = "per_trade" → backtest 用 ACCOUNT_CAPITAL/COST_PER_SHARE/MIN_PER_ORDER
# COMMISSION_MODEL = "uniform"   → backtest 用 TRANSACTION_COST × turnover (legacy)
COMMISSION_MODEL = "per_trade"
ACCOUNT_CAPITAL  = 100_000       # USD，per-trade 模式假設的帳戶資金
COST_PER_SHARE   = 0.0035        # IB Pro Tiered: $0.0035/share (Fixed 用 0.005)
MIN_PER_ORDER    = 0.35          # IB Pro Tiered: $0.35/order  (Fixed 用 1.00)

# Legacy uniform-mode 成本，COMMISSION_MODEL="uniform" 時生效
TRANSACTION_COST = 0.0001        # 1 bps 單向 (MOO 真實估計，舊版用 0.00075)

# ── 資料下載 ──────────────────────────────────────────────────────────────
# yfinance start date：給夠遠，yfinance 自動回傳該 ticker 真實 inception 之後的資料
# 載入時會自動取所有 ticker 的共同最早日期 (= 最晚發行日)
DOWNLOAD_START = "2010-01-01"
