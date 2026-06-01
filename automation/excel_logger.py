"""
QuantCore Automation — Excel 日誌記錄
  portfolio_tracker.xlsx  — 每日收盤持倉快照（一行一標的，含 Strategy heuristic）
  trade_log.xlsx          — 每次調倉委託記錄（Strategy 由 order dict 帶入）
"""

from pathlib import Path
from datetime import datetime, date
import pandas as pd

BASE_DIR       = Path(__file__).parent
PORTFOLIO_FILE = BASE_DIR / 'portfolio_tracker.xlsx'
TRADE_FILE     = BASE_DIR / 'trade_log.xlsx'
PORT_SHEET     = 'Daily'
TRADE_SHEET    = 'Trades'

# Strategy classification 用 (與 daily_report 對齊)
_SR_ETFS = {'XLK', 'XLF', 'XLV', 'XLE', 'XLI', 'XLY', 'XLB',
            'XLP', 'XLU', 'XLC', 'XLRE'}
# 確保 strategies 套件可 import — 即使 excel_logger 被獨立 import
import sys as _sys
_ROOT_DIR = BASE_DIR.parent
if str(_ROOT_DIR) not in _sys.path:
    _sys.path.insert(0, str(_ROOT_DIR))
try:
    from strategies.sector_skew_ls import config as _skewls_cfg
    _SKEWLS_INVERSE_TICKERS = set(_skewls_cfg.INVERSE_TICKERS)
except Exception:
    _SKEWLS_INVERSE_TICKERS = set()


def _classify_strategy(symbol: str, position: float) -> str:
    """
    依 symbol + 部位方向猜策略歸屬（用於 daily snapshot Strategy 欄位）。
      - INVERSE_TICKERS (REW/SEF/...) → SkewLS
      - SR_ETF 且 position<0 → SkewLS (sector short, 如 XLC)
      - SR_ETF 且 position>0 → SR (可能含 SkewLS overlap, 但 SR 為主要歸屬)
      - 其他 → '' (空字串；由 caller 視情況補 'SP500' 或 'Other')
    """
    if symbol in _SKEWLS_INVERSE_TICKERS:
        return "SkewLS"
    if symbol in _SR_ETFS and position < 0:
        return "SkewLS"
    if symbol in _SR_ETFS:
        return "SR"
    return ""    # caller 自行決定 SP500 / Other


# ── 通用：附加到 Excel ────────────────────────────────────────────────────────
def _append(filepath: Path, sheet: str, rows: list) -> None:
    """將 rows（list of dict）附加到 Excel 工作表；不存在則新建含標頭的檔案。

    若檔案已存在，會依舊檔欄序對齊 rows；遇到 dict 有但舊檔沒有的新欄位時，
    以 dict 欄序為準補欄並整檔重寫 (header migration)，避免欄位錯位。
    """
    if not rows:
        return
    df_new = pd.DataFrame(rows)

    if not filepath.exists():
        with pd.ExcelWriter(filepath, engine='openpyxl') as w:
            df_new.to_excel(w, sheet_name=sheet, index=False)
        return

    existing = pd.read_excel(filepath, sheet_name=sheet)
    # 丟掉 pandas 為空白尾欄產生的 'Unnamed: N' (使用者誤加的空白欄)
    existing = existing.loc[:, ~existing.columns.astype(str).str.startswith('Unnamed:')]

    new_in_dict = [c for c in df_new.columns if c not in existing.columns]

    if new_in_dict:
        # 結構變更：以 dict 欄序為準，把缺欄補進舊資料後整檔重寫
        ordered = list(df_new.columns) + [c for c in existing.columns
                                          if c not in df_new.columns]
        existing = existing.reindex(columns=ordered)
        df_new   = df_new.reindex(columns=ordered)
        combined = pd.concat([existing, df_new], ignore_index=True)
        with pd.ExcelWriter(filepath, engine='openpyxl', mode='w') as w:
            combined.to_excel(w, sheet_name=sheet, index=False)
    else:
        # 沿用舊欄序；df_new 缺的欄位以 NaN 補
        df_new = df_new.reindex(columns=list(existing.columns))
        with pd.ExcelWriter(filepath, engine='openpyxl', mode='a',
                            if_sheet_exists='overlay') as w:
            df_new.to_excel(w, sheet_name=sheet, index=False,
                            header=False, startrow=len(existing) + 1)


# ── 每日持倉快照 ──────────────────────────────────────────────────────────────
def append_daily_snapshot(nav: float, pos_detail: list, prices: dict,
                          cushion_pct: float = 0.0, excess_liq: float = 0.0,
                          account_name: str = "") -> None:
    today = date.today().isoformat()
    rows = []
    for p in pos_detail:
        sym      = p.contract.symbol
        pos      = p.position
        avg_cost = p.avgCost
        mkt_px   = prices.get(sym, 0)
        mkt_val  = pos * mkt_px                                   # short 時為負
        upnl     = mkt_val - pos * avg_cost                       # long/short 通用
        cost_abs = abs(pos * avg_cost)
        upnl_pct = round(upnl / cost_abs * 100, 2) if cost_abs else 0
        row = {
            'Date':       today,
            'Account':    account_name,
            'NAV':        round(nav, 2),
            'Cushion%':   round(cushion_pct, 2),
            'ExcessLiq':  round(excess_liq, 2),
            'Symbol':     sym,
            'Strategy':   _classify_strategy(sym, pos),    # S21: heuristic 歸屬
            'Shares':     round(pos, 4),
            'AvgCost':    round(avg_cost, 4),
            'MktPx':      round(mkt_px, 4),
            'MktVal':     round(mkt_val, 2),
            'UnrealPnL':  round(upnl, 2),
            'UnrealPnL%': upnl_pct,
        }
        rows.append(row)
    _append(PORTFOLIO_FILE, PORT_SHEET, rows)


# ── 交易記錄 ──────────────────────────────────────────────────────────────────
def append_trade_log(placed: list, fill_results: list, signal_date,
                     order_type: str, leverage: float, dry_run: bool,
                     strategy: str = "", account_name: str = "") -> None:
    now      = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today    = date.today().isoformat()
    fill_map = {f['symbol']: f for f in fill_results}
    rows     = []

    for o in placed:
        sym    = o['symbol']
        qty    = int(o['qty'])
        est_px = o.get('lmt_price', 0)
        fill   = fill_map.get(sym, {})

        fill_qty    = fill.get('filled', 0) if fill else 0
        fill_px     = fill.get('avg_price', 0) if fill else 0
        fill_val    = round(fill_qty * fill_px, 2) if fill_qty else ''
        fill_status = fill.get('status', 'DRY_RUN') if fill else 'DRY_RUN'

        rows.append({
            'DateTime':   now,
            'Date':       today,
            'Account':    account_name,
            'SignalDate': str(signal_date),
            'Strategy':   o.get('strategy', strategy),
            'Symbol':     sym,
            'Action':     o['action'],
            'Qty':        qty,
            'EstPrice':   round(est_px, 4),
            'EstValue':   round(qty * est_px, 2),
            'FillPrice':  round(fill_px, 4) if fill_px else '',
            'FillQty':    fill_qty if fill_qty else '',
            'FillValue':  fill_val,
            'FillStatus': fill_status,
            'OrderType':  order_type,
            'Leverage':   leverage,
            'DryRun':     dry_run,
        })
    _append(TRADE_FILE, TRADE_SHEET, rows)
