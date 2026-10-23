"""
QuantCore Automation — Excel 日誌記錄
  portfolio_tracker.xlsx  — 每日收盤持倉快照（一行一標的，含 Strategy heuristic）
  trade_log.xlsx          — 每次調倉委託記錄（Strategy 由 order dict 帶入）
讀取端是 `ledger`（#209）；兩邊的欄位名稱都用這裡的常數。
"""

from pathlib import Path
from datetime import datetime, date
import json
import logging
import os
from uuid import uuid4
import pandas as pd

log = logging.getLogger(__name__)

BASE_DIR       = Path(__file__).parent
PORTFOLIO_FILE = BASE_DIR / 'portfolio_tracker.xlsx'
TRADE_FILE     = BASE_DIR / 'trade_log.xlsx'
SQLITE_FILE    = BASE_DIR / 'quantcore_logs.sqlite3'
CONFIG_FILE    = BASE_DIR / 'config.json'      # 後端設定 log_backend（#230）
BACKEND_ENV    = 'QUANTCORE_LOG_BACKEND'       # 過渡期覆寫 config.json
PORT_SHEET     = 'Daily'
TRADE_SHEET    = 'Trades'

# ── 欄位名稱（#209）：寫端在這裡、讀端 `ledger`、`sqlite_store` 的對照都用這一組 ──
DATE, ACCOUNT, STRATEGY, SYMBOL = 'Date', 'Account', 'Strategy', 'Symbol'
# portfolio_tracker.xlsx（Daily）
NAV, CUSHION_PCT, EXCESS_LIQ = 'NAV', 'Cushion%', 'ExcessLiq'
SHARES, AVG_COST, MKT_PX, MKT_VAL = 'Shares', 'AvgCost', 'MktPx', 'MktVal'
UNREAL_PNL, UNREAL_PNL_PCT = 'UnrealPnL', 'UnrealPnL%'
# trade_log.xlsx（Trades）
DATETIME, SIGNAL_DATE, ACTION, QTY = 'DateTime', 'SignalDate', 'Action', 'Qty'
EST_PRICE, EST_VALUE = 'EstPrice', 'EstValue'
FILL_PRICE, FILL_QTY, FILL_VALUE, FILL_STATUS = 'FillPrice', 'FillQty', 'FillValue', 'FillStatus'
ORDER_TYPE, LEVERAGE, DRY_RUN = 'OrderType', 'Leverage', 'DryRun'

# 新檔的欄序
SNAPSHOT_FIELDS = (
    DATE, ACCOUNT, NAV, CUSHION_PCT, EXCESS_LIQ, SYMBOL, STRATEGY, SHARES,
    AVG_COST, MKT_PX, MKT_VAL, UNREAL_PNL, UNREAL_PNL_PCT,
)
TRADE_FIELDS = (
    DATETIME, DATE, ACCOUNT, SIGNAL_DATE, STRATEGY, SYMBOL, ACTION, QTY,
    EST_PRICE, EST_VALUE, FILL_PRICE, FILL_QTY, FILL_VALUE, FILL_STATUS,
    ORDER_TYPE, LEVERAGE, DRY_RUN,
)

from symbol_contract import canonical_symbol


def _classify_strategy(symbol: str, position: float,
                       alloc: tuple[float, float] | None = None) -> str:
    """
    依 symbol + 部位方向猜策略歸屬（用於 daily snapshot Strategy 欄位）。
    規則與新 Panel 持倉表共用 `strategy_owner.etf_owner`（#181）：
      - 反向 ETF、做空的類股 ETF → SkewLS
      - 做多的類股 ETF → 依 alloc=(alloc_sr, alloc_skewls)：SR 0% → SkewLS；
        兩個都開 → SR+SkewLS；沒給 alloc → SR（舊規則）
      - 其他 → '' (空字串；由 caller 視情況補 'SP500' 或 'Other')
    """
    from strategy_owner import etf_owner
    return etf_owner(canonical_symbol(symbol), position, alloc)


# ── 通用：附加到 Excel ────────────────────────────────────────────────────────
def _append_excel(filepath: Path, sheet: str, rows: list) -> None:
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


_announced: set[tuple[str, str]] = set()        # 已寫過 log 的 (後端, 來源)


def _log_backend() -> str:
    """交易紀錄寫去哪個後端（`excel`／`sqlite`），只在一處決定（#230）。

    依序：環境變數 QUANTCORE_LOG_BACKEND（過渡期覆寫，live 既有的 wrapper 排程動作
    不壞）→ config.json 的 `log_backend` → 預設 excel。Panel 與排程都讀同一份
    config.json；每個行程第一次用到（或後端改變）時寫 log 說明後端與來源。
    """
    backend, source = os.getenv(BACKEND_ENV, '').strip(), f'環境變數 {BACKEND_ENV}'
    if not backend:
        backend, source = _configured_backend(), 'config.json 的 log_backend'
    if not backend:
        backend, source = 'excel', '預設'
    backend = backend.casefold()
    if (backend, source) not in _announced:
        _announced.add((backend, source))
        log.info(f'交易紀錄後端：{backend}（{source}）')
    return backend


def _configured_backend() -> str:
    """config.json 的 `log_backend`；沒有檔或沒有欄位回空字串。

    檔案在但讀不懂（不是 JSON、不是物件、欄位不是字串）就報錯並說明原因：
    猜一個後端寫下去，比寫不進去更難收拾。
    """
    if not CONFIG_FILE.exists():
        return ''
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f'{CONFIG_FILE} 讀不到，無法決定交易紀錄後端：{e}') from e
    if not isinstance(cfg, dict):
        raise ValueError(f'{CONFIG_FILE} 不是 JSON 物件，無法決定交易紀錄後端')
    value = cfg.get('log_backend')
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError(f'{CONFIG_FILE} 的 log_backend 要是 "excel" 或 "sqlite"，不是 {value!r}')
    return value.strip()


def _ensure_sqlite_store():
    """Initialize SQLite, importing the legacy workbooks on first use."""
    import sqlite_store

    if SQLITE_FILE.exists():
        sqlite_store.initialize(SQLITE_FILE)
    else:
        sqlite_store.import_excel(
            SQLITE_FILE,
            PORTFOLIO_FILE if PORTFOLIO_FILE.exists() else None,
            TRADE_FILE if TRADE_FILE.exists() else None,
        )
    return sqlite_store


def _append_rows(sheet: str, rows: list, identities: list[str]) -> None:
    """Write rows to the selected backend and refresh Excel compatibility files."""
    if not rows:
        return
    backend = _log_backend()
    if backend == 'excel':
        _append_excel(PORTFOLIO_FILE if sheet == PORT_SHEET else TRADE_FILE,
                      sheet, rows)
        return
    if backend != 'sqlite':
        raise ValueError(f'unsupported log backend: {backend!r}（config.json 的 log_backend '
                         f'或 {BACKEND_ENV} 只能是 excel／sqlite）')

    sqlite_store = _ensure_sqlite_store()
    frame = pd.DataFrame(rows)
    if sheet == PORT_SHEET:
        sqlite_store.append_runtime(
            SQLITE_FILE, snapshots=frame, snapshot_identities=identities,
        )
    else:
        sqlite_store.append_runtime(
            SQLITE_FILE, trades=frame, trade_identities=identities,
        )
    # Excel remains a generated compatibility projection for the panel and
    # reports.  The database commit occurs before this export.
    sqlite_store.export_excel(SQLITE_FILE, PORTFOLIO_FILE, TRADE_FILE)


# ── 每日持倉快照 ──────────────────────────────────────────────────────────────
def append_daily_snapshot(nav: float, pos_detail: list, prices: dict,
                          cushion_pct: float = 0.0, excess_liq: float = 0.0,
                          account_name: str = "",
                          capture_id: str | None = None,
                          strategy_alloc: tuple[float, float] | None = None) -> None:
    """strategy_alloc: 帳戶的 (alloc_sr, alloc_skewls)，決定做多類股 ETF 歸屬（#181）。"""
    today = date.today().isoformat()
    rows = []
    for p in pos_detail:
        sym      = canonical_symbol(p.contract.symbol)
        pos      = p.position
        avg_cost = p.avgCost
        mkt_px   = prices.get(sym, 0)
        mkt_val  = pos * mkt_px                                   # short 時為負
        upnl     = mkt_val - pos * avg_cost                       # long/short 通用
        cost_abs = abs(pos * avg_cost)
        upnl_pct = round(upnl / cost_abs * 100, 2) if cost_abs else 0
        row = {
            DATE:           today,
            ACCOUNT:        account_name,
            NAV:            round(nav, 2),
            CUSHION_PCT:    round(cushion_pct, 2),
            EXCESS_LIQ:     round(excess_liq, 2),
            SYMBOL:         sym,
            STRATEGY:       _classify_strategy(sym, pos, strategy_alloc),  # heuristic 歸屬
            SHARES:         round(pos, 4),
            AVG_COST:       round(avg_cost, 4),
            MKT_PX:         round(mkt_px, 4),
            MKT_VAL:        round(mkt_val, 2),
            UNREAL_PNL:     round(upnl, 2),
            UNREAL_PNL_PCT: upnl_pct,
        }
        rows.append(row)
    capture = str(capture_id or uuid4().hex)
    identities = [
        f"snapshot:{capture}:{account_name}:{row[SYMBOL]}"
        for row in rows
    ]
    _append_rows(PORT_SHEET, rows, identities)


# ── 交易記錄 ──────────────────────────────────────────────────────────────────
def append_trade_log(placed: list, fill_results: list, signal_date,
                     order_type: str, leverage: float, dry_run: bool,
                     strategy: str = "", account_name: str = "",
                     capture_id: str | None = None) -> None:
    now      = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today    = date.today().isoformat()
    fill_map = {canonical_symbol(f['symbol']): f for f in fill_results}
    rows     = []

    for o in placed:
        sym    = canonical_symbol(o['symbol'])
        qty    = int(o['qty'])
        est_px = o.get('lmt_price', 0)
        fill   = fill_map.get(sym, {})

        fill_qty    = fill.get('filled', 0) if fill else 0
        fill_px     = fill.get('avg_price', 0) if fill else 0
        fill_val    = round(fill_qty * fill_px, 2) if fill_qty else ''
        fill_status = fill.get('status', 'DRY_RUN') if fill else 'DRY_RUN'

        rows.append({
            DATETIME:    now,
            DATE:        today,
            ACCOUNT:     account_name,
            SIGNAL_DATE: str(signal_date),
            STRATEGY:    o.get('strategy', strategy),
            SYMBOL:      sym,
            ACTION:      o['action'],
            QTY:         qty,
            EST_PRICE:   round(est_px, 4),
            EST_VALUE:   round(qty * est_px, 2),
            FILL_PRICE:  round(fill_px, 4) if fill_px else '',
            FILL_QTY:    fill_qty if fill_qty else '',
            FILL_VALUE:  fill_val,
            FILL_STATUS: fill_status,
            ORDER_TYPE:  order_type,
            LEVERAGE:    leverage,
            DRY_RUN:     dry_run,
        })
    capture = str(capture_id or uuid4().hex)
    identities = []
    for index, order in enumerate(placed):
        # A broker order id is stable across a retry when available.  Dry-run
        # and panel callers can pass capture_id explicitly; otherwise the
        # generated call id intentionally represents a new log event.
        order_id = order.get('idempotency_key') or order.get('order_id')
        identity = (f"trade:{account_name}:{order_id}" if order_id
                    else f"trade:{account_name}:{capture}:{index}")
        identities.append(identity)
    _append_rows(TRADE_SHEET, rows, identities)
