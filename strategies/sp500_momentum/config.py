"""
SP500 Momentum — 參數設定
所有可調參數集中在此，其他模組從這裡 import。
"""

from pathlib import Path

# ── 資料路徑 ──────────────────────────────────────────────────
_ROOT     = Path(__file__).resolve().parent.parent.parent
STOCK_DIR = _ROOT / "data" / "stocks"
ETF_DIR   = _ROOT / "data" / "etfs"
REF_DIR   = _ROOT / "data" / "reference"

# 參考檔案
CONST_FILE     = "sp500_constituents_daily.xlsx"   # ADR-0002 refresh output (old *_2015_2026 was frozen at 2026-01-14)
TRADING_DAYS_FILE = "us_trading_days.xlsx"

# ── 回測時間範圍 ───────────────────────────────────────────────
START_DATE   = "2018-07-01"
END_DATE     = None          # None = 自動偵測資料最後日期

# ── 資金設定 ──────────────────────────────────────────────────
INITIAL_CASH = 1_000_000

# ── 交易成本 ──────────────────────────────────────────────────
# IB 全成本估算（含點差）0.05% × 1.5 = 0.075%（單向）
COMMISSION = 0.00075

# ── 選股參數 ──────────────────────────────────────────────────
TARGET_HOLDINGS   = 4     # 目標持股數
LOOKBACK          = 90    # 動能排名計算週期（天）
LOOKBACK_ENTRY    = LOOKBACK
LOOKBACK_EXIT     = LOOKBACK
EXIT_EMA          = 50    # 跌破此 EMA 出場
SELL_RANK_THRESHOLD = 20  # 排名跌出前 N 名則賣出
ENTRY_EMA_FILTER  = False # 買入候選是否排除「收盤<EXIT_EMA」者。WF 否決（ADR-0005）：
                          # rank 優先 — 高排名急跌股續抱/買回在 OOS 勝出；開關保留供重驗
MAX_ADJ_SLOPE     = 1.5   # 過熱保護：Adj Slope 上限

# ── 噴出進場封鎖（Blowoff entry block，ADR-0011）───────────────
# 「漲相不穩」過濾：候選股 60 日日報酬 kurtosis > 門檻者不佔補位名額，
# 與 MAX_ADJ_SLOPE（漲太快）並列的第二道進場濾網 — 只擋買、不砍倉。
# 門檻 = 2018-07→2026-07-09 獲利中持股 kurt60 分布 P90 錨定；WF 過 gate
# （kurt60/skew60 × N=8/N=7 全 PASS，每折重錨門檻 10~22 漂移皆穩定勝出）。
# 研究：robustness/takeprofit_report.md；同源定義 robustness/takeprofit_common.py。
# 翻 flag = 策略變更，須重跑 WF 過 gate 才能改（比照 ENTRY_EMA_FILTER 紀律）。
BLOWOFF_ENTRY_BLOCK = True    # 2026-07-11 上線（ADR-0011；characterization 全過）
BLOWOFF_KURT_WINDOW = 60      # 日報酬 rolling kurtosis 窗口（WF 驗證值，勿單獨調）
BLOWOFF_KURT_MAX    = 14.7712 # P90 錨定值（anchor_thresholds.json 2026-07-10）

# ── 風險管理 ──────────────────────────────────────────────────
STOP_LOSS_PCT     = 0.20  # DEPRECATED/未使用 — 停損已移除（ADR-0004）
SKIP_MAX_GAP_PCT  = 0.20  # 跳空 > 20% 不買入（進場過濾，保留）
GAP_EXIT_PCT      = 0.50  # DEPRECATED/未使用 — 跳空出場已移除（ADR-0004）
REBALANCE_THRESHOLD = 0.03  # 超重/低配 >= 3% 才調整
MIN_BUY_AMOUNT_PCT  = 0.03  # 單次補倉最低為總權益 3%

# ── 再平衡設定 ────────────────────────────────────────────────
REBALANCE_WEEKDAY = 2     # 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri
REBALANCE_WEEKS   = 1     # 每幾週再平衡一次

# ── 基準與熊市抄底 ────────────────────────────────────────────
BENCHMARK_TICKER    = "SPY"
DIP_BUY_TICKER      = "SPY"    # 熊市抄底標的（從 SSO 改為 SPY，2026-03 高利率環境）
ENABLE_BEAR_DIP_BUY = True     # 開啟熊市 SSO 抄底策略

# 熊市 DD 階層配置：{DD_level: allocation_of_strategy_capital}
# 各階層獨立觸發，同一等級只觸發一次（直到牛市確認後 reset）
# 總和可達 100% → 熊市全倉抄底
DIP_BUY_ALLOCATIONS = {
    0.15: 0.30,   # SPY 自高點回落 15% → 配置 30%
    0.20: 0.30,   # 再跌到 20%         → 再配置 30%
    0.25: 0.40,   # 再跌到 25%         → 再配置 40%（總和 100%）
}

# ── ATR 參數 ──────────────────────────────────────────────────
ATR_PERIOD = 20    # ATR 計算週期

# ── 黑名單 ────────────────────────────────────────────────────
# 格式：[("YYYY-MM-DD", "TICKER"), ...]  日期後該股進黑名單
BLACKLIST = [
    ("2025-12-31", "WBD"),    # 2025/12/31 後不持有 WBD
]

# ── 類股（Constituents 寫法含 "."，例 BRK.B / BF.B）───────────────
# 行情 reader：BRK.B → BRK-B.csv（#50 命名契約）。
# IB adapter：BRK.B → BRK B（automation/symbol_contract.py，#52）。
# 類股已可安全進入選股與下單流程。
CLASS_SHARES_TRADABLE = True

# ── 輸出設定 ──────────────────────────────────────────────────
OUTPUT_DIR = _ROOT / "output"
LIVE_MODE  = True     # True = 回測結束後保留持倉（mark-to-market 計算最終權益）
