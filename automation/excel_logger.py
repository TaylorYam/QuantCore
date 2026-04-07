"""
QuantCore Automation — Excel 日誌記錄
  portfolio_tracker.xlsx  — 每日收盤持倉快照（一行一標的）
  trade_log.xlsx          — 每次調倉委託記錄
"""

from pathlib import Path
from datetime import datetime, date
import pandas as pd

BASE_DIR       = Path(__file__).parent
PORTFOLIO_FILE = BASE_DIR / 'portfolio_tracker.xlsx'
TRADE_FILE     = BASE_DIR / 'trade_log.xlsx'
PORT_SHEET     = 'Daily'
TRADE_SHEET    = 'Trades'


# ── 通用：附加到 Excel ────────────────────────────────────────────────────────
def _append(filepath: Path, sheet: str, rows: list) -> None:
    """將 rows（list of dict）附加到 Excel 工作表；不存在則新建含標頭的檔案。"""
    if not rows:
        return
    df_new = pd.DataFrame(rows)

    if filepath.exists():
        existing = pd.read_excel(filepath, sheet_name=sheet)
        with pd.ExcelWriter(filepath, engine='openpyxl', mode='a',
                            if_sheet_exists='overlay') as w:
            df_new.to_excel(w, sheet_name=sheet, index=False,
                            header=False, startrow=len(existing) + 1)
    else:
        with pd.ExcelWriter(filepath, engine='openpyxl') as w:
            df_new.to_excel(w, sheet_name=sheet, index=False)


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
        mkt_val  = pos * mkt_px
        upnl     = mkt_val - pos * avg_cost
        upnl_pct = round(upnl / (pos * avg_cost) * 100, 2) if avg_cost else 0
        row = {
            'Date':       today,
            'Account':    account_name,
            'NAV':        round(nav, 2),
            'Cushion%':   round(cushion_pct, 2),
            'ExcessLiq':  round(excess_liq, 2),
            'Symbol':     sym,
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
