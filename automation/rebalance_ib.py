#!/usr/bin/env python3
"""
QuantCore — IB Gateway 自動調倉腳本
80% Sector Rotation + 50% SP500 Momentum = 130% exposure (1.3x leverage)

兩個策略各自獨立排程，與回測一致：
  週二 21:00 → python rebalance_ib.py --mode sr      (Sector Rotation)
  週三 21:00 → python rebalance_ib.py --mode sp500   (SP500 Momentum)
  手動全部  → python rebalance_ib.py --mode both     (兩個一起)

使用方式：
  1. 修改 config.json（帳號、port、dry_run、order_type）
  2. python rebalance_ib.py --mode sr     # 只跑 SR
  3. python rebalance_ib.py --mode sp500  # 只跑 SP500
  4. python rebalance_ib.py --mode both   # 兩個都跑（預設）
  5. 確認 log 無誤後改 dry_run=false

多帳戶模式：
  python rebalance_ib.py --mode sr --account A     # 指定帳戶
  python rebalance_ib.py --mode sr --account all   # 所有帳戶循序執行

order_type 選項：
  "MKT"  - 市價單（盤中任何時間）
  "MOO"  - Market-on-Open，保證開盤價成交，須在 09:25 ET 前送出
  "LMT"  - 限價單（需搭配 outside_rth=true 可做盤前盤後）
"""

import argparse
import html as html_mod
import json, logging, time, sys, urllib.request, urllib.parse
from pathlib import Path
from datetime import date, datetime

import matplotlib
matplotlib.use('Agg')
import numpy as np
import pandas as pd

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
ROOT_DIR  = BASE_DIR.parent          # C:/QuantCore-master
LOG_DIR   = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

# 加入 QuantCore root 以便 import strategies
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from excel_logger import append_trade_log

# ── 日誌設定 ──────────────────────────────────────────────────────────────────
log_file = LOG_DIR / f"rebalance_{date.today().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)s  %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
log = logging.getLogger(__name__)

# ── 讀取設定 ──────────────────────────────────────────────────────────────────
cfg_path = BASE_DIR / 'config.json'
with open(cfg_path, encoding='utf-8') as f:
    cfg = json.load(f)

HOST           = cfg.get('host',           '127.0.0.1')
PORT           = cfg.get('port',           7497)
CLIENT_ID      = cfg.get('client_id',      20)
ACCOUNT        = cfg.get('account',        '')
ACCOUNT_NAME   = cfg.get('account_name',   '')       # 多帳戶用
DRY_RUN        = cfg.get('dry_run',        True)
ORDER_TYPE     = cfg.get('order_type',     'MOO')
OUTSIDE_RTH    = cfg.get('outside_rth',    False)
WAIT_FILL_SEC  = cfg.get('wait_fill_sec',  30)
ALLOC_SR       = cfg.get('alloc_sr',       0.80)
ALLOC_SP500    = cfg.get('alloc_sp500',    0.50)
LEVERAGE       = cfg.get('leverage',       1.3)
TG_TOKEN       = cfg.get('telegram_token',   '')
TG_CHAT_ID     = cfg.get('telegram_chat_id', '')
# 即時報價備援：新帳戶若無 market data 訂閱，會自動連到這個 port 抓價
# 例如 Rosa 有訂閱 → 設為 Rosa 的 port (7497)
PRICE_FEED_PORT = cfg.get('price_feed_port', 7497)


def _apply_config(cfg_dict: dict) -> None:
    """將 config dict 套用到模組全域變數（多帳戶切換用）"""
    global HOST, PORT, CLIENT_ID, ACCOUNT, ACCOUNT_NAME, DRY_RUN, ORDER_TYPE
    global OUTSIDE_RTH, WAIT_FILL_SEC, ALLOC_SR, ALLOC_SP500, LEVERAGE
    global TG_TOKEN, TG_CHAT_ID, PRICE_FEED_PORT
    HOST          = cfg_dict.get('host',           '127.0.0.1')
    PORT          = cfg_dict.get('port',           7497)
    ACCOUNT       = cfg_dict.get('account',        '')
    ACCOUNT_NAME  = cfg_dict.get('account_name',   '')
    DRY_RUN       = cfg_dict.get('dry_run',        True)
    ORDER_TYPE    = cfg_dict.get('order_type',     'MOO')
    OUTSIDE_RTH   = cfg_dict.get('outside_rth',    False)
    WAIT_FILL_SEC = cfg_dict.get('wait_fill_sec',  30)
    ALLOC_SR      = cfg_dict.get('alloc_sr',       0.80)
    ALLOC_SP500   = cfg_dict.get('alloc_sp500',    0.50)
    LEVERAGE      = cfg_dict.get('leverage',       1.3)
    TG_TOKEN      = cfg_dict.get('telegram_token',   '')
    TG_CHAT_ID    = cfg_dict.get('telegram_chat_id', '')
    PRICE_FEED_PORT = cfg_dict.get('price_feed_port', 7497)
    # clientId 由 account_utils 計算
    from account_utils import get_client_id, ROLE_REBALANCE
    CLIENT_ID     = get_client_id(ACCOUNT_NAME, ROLE_REBALANCE) if ACCOUNT_NAME else 20

# ── IB 連線工具 ───────────────────────────────────────────────────────────────
from ib_insync import IB, Stock, MarketOrder, LimitOrder, Order, util
util.logToConsole(logging.WARNING)
logging.root.setLevel(logging.INFO)


# ── Telegram 通知 ─────────────────────────────────────────────────────────────
def tg_send(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id':    TG_CHAT_ID,
            'text':       msg,
            'parse_mode': 'HTML',
        }).encode()
        req = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                log.warning(f"Telegram HTTP {resp.status}")
    except Exception as e:
        log.warning(f"Telegram notify failed: {e}")


# ── IB 工具函式 ───────────────────────────────────────────────────────────────
def connect_ib() -> IB:
    ib = IB()
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=20)
    log.info(f"Connected  host={HOST} port={PORT} clientId={CLIENT_ID} account={ACCOUNT}")
    return ib


def get_nav(ib: IB) -> float:
    for v in ib.accountValues(ACCOUNT):
        if v.tag == 'NetLiquidation' and v.currency == 'USD':
            return float(v.value)
    raise RuntimeError("Cannot get NAV — check account ID in config.json")


def get_positions(ib: IB) -> dict:
    return {
        p.contract.symbol: p.position
        for p in ib.positions(ACCOUNT)
        if p.contract.secType == 'STK'
    }


def get_positions_detail(ib: IB) -> list:
    return [
        p for p in ib.positions(ACCOUNT)
        if p.contract.secType == 'STK' and p.position != 0
    ]


def get_prices(ib: IB, symbols: list) -> dict:
    """
    取得 symbols 的報價（透過 price_utils.robust_get_prices）。
    優先順序：本連線即時 → price_feed_port → delayed → 本地 CSV。
    """
    from price_utils import robust_get_prices
    return robust_get_prices(
        ib, symbols,
        host=HOST,
        price_feed_port=PRICE_FEED_PORT,
        verbose_log=lambda m: log.info(m),
    )


def make_order(action: str, qty: int, lmt_price: float) -> Order:
    if ORDER_TYPE == 'MOO':
        o = Order()
        o.action        = action
        o.totalQuantity = qty
        o.orderType     = 'MKT'
        o.tif           = 'OPG'
        o.outsideRth    = False
    elif ORDER_TYPE == 'LMT':
        o = LimitOrder(action, qty, lmt_price)
        o.outsideRth = OUTSIDE_RTH
    else:  # MKT
        o = MarketOrder(action, qty)
        o.outsideRth = OUTSIDE_RTH
        if OUTSIDE_RTH:
            o.tif = 'GTC'
    # FA 帳戶必須指定下單帳戶，否則 Error 436
    if ACCOUNT:
        o.account = ACCOUNT
    return o


def place_orders(ib: IB, order_list: list) -> tuple:
    """先 SELL 再 BUY。回傳 (placed_list, trades_list)"""
    sells = [o for o in order_list if o['action'] == 'SELL']
    buys  = [o for o in order_list if o['action'] == 'BUY']
    placed, trades = [], []

    for o in sells + buys:
        sym = o['symbol']
        qty = int(abs(o['qty']))
        if qty == 0:
            continue

        contract = Stock(sym, 'SMART', 'USD')
        ib.qualifyContracts(contract)
        order = make_order(o['action'], qty, o.get('lmt_price', 0))

        if DRY_RUN:
            log.info(f"  [DRY RUN] {ORDER_TYPE} {o['action']:4s} {qty:5d} {sym}"
                     f"  ({o.get('strategy', '')})")
        else:
            trade = ib.placeOrder(contract, order)
            trades.append(trade)
            log.info(f"  {ORDER_TYPE} {o['action']:4s} {qty:5d} {sym}"
                     f"  orderId={trade.order.orderId}  ({o.get('strategy', '')})")
        placed.append(o)
        time.sleep(0.2)

    if not DRY_RUN and placed:
        log.info(f"Waiting {WAIT_FILL_SEC}s for fills...")
        ib.sleep(WAIT_FILL_SEC)

    return placed, trades


def check_fills(trades: list) -> list:
    results = []
    for trade in trades:
        sym     = trade.contract.symbol
        action  = trade.order.action
        qty     = int(trade.order.totalQuantity)
        filled  = sum(f.execution.shares for f in trade.fills)
        avg_px  = (
            sum(f.execution.shares * f.execution.avgPrice for f in trade.fills) / filled
            if filled > 0 else 0.0
        )
        status  = trade.orderStatus.status
        log.info(f"  Fill: {action} {sym}  {filled}/{qty}sh @ ${avg_px:.2f}  [{status}]")
        results.append({
            'symbol':    sym,
            'action':    action,
            'qty':       qty,
            'filled':    filled,
            'avg_price': avg_px,
            'status':    status,
        })
    return results


# ── 策略訊號計算 ──────────────────────────────────────────────────────────────

def compute_sr_weights() -> tuple[dict, str]:
    """
    取 Sector Rotation 最新目標權重（live 模式）。

    使用 get_latest_weights_live() 直接從最新資料算訊號，
    不套用 rebalance mask / forward-fill，避免週二盤前拿到上週舊訊號。
    等同回測中「今天是 rebalance day」時的 shift(1) 結果。

    回傳 (weights_dict, signal_date_str)
    """
    from strategies.sector_rotation.data_loader import load_data
    from strategies.sector_rotation.signal import get_latest_weights_live

    data = load_data()
    etf_close = data['etf_close']
    weights, sig_date = get_latest_weights_live(etf_close)

    wgt = {sym: float(w) for sym, w in weights.items() if w > 0.001}
    log.info(f"SR signal date: {sig_date.date()}  (live, skip rebal mask)")
    for sym, w in sorted(wgt.items(), key=lambda x: -x[1]):
        log.info(f"  SR  {sym}: {w*100:.0f}%")
    return wgt, str(sig_date.date())


def compute_sp500_weights(current_sp500_holdings: list[str],
                          account_name: str = "") -> tuple[dict, str]:
    """
    基於 IB 帳戶的實際 SP500 持股，產生 live 目標權重（與 backtester 一致）。

    對齊回測的邏輯：
      1. 牛市確認：連續 2 個 rebal day SPY>MA200 才做 rotation
         （bull_weeks_counter，持久化於 sp500_state_{account}.json）
      2. 未確認牛市 + SPY < MA200 → 熊市，執行 dip-buy（15/20/25% DD）
      3. 未確認牛市 + SPY ≥ MA200 → 等待，不動作
      4. REBALANCE_WEEKS gate：只有 rotation week 才執行 rotation
      5. rotation 流程：保留 top 20 內且未跌破 EMA50 的持股，補滿 TARGET_HOLDINGS
      6. ATR 反比例權重

    Parameters
    ----------
    current_sp500_holdings : list[str]
        IB 帳戶中屬於 SP500 策略的股票代碼（不含 SR ETFs）
    account_name : str
        帳戶名稱，用於讀取 / 寫入 state file

    Returns
    -------
    (weights_dict, signal_date_str)

    Note
    ----
    當回傳空 dict 時可能是下列情況之一：
      - 熊市未滿足 dip-buy 條件（權重由熊市邏輯處理）
      - 牛市尚未確認且 SPY ≥ MA200 (holding pattern)
    主流程會根據 SR+SP500 合併結果計算 orders。
    """
    from strategies.sp500_momentum.selection    import SelectionEngine
    from strategies.sp500_momentum.market_regime import MarketRegime
    from strategies.sp500_momentum import config as sp_cfg, utils
    from sp500_state import (load_state, save_state, update_bull_counter,
                             is_bull_confirmed, reset_dip_state,
                             is_dip_triggered, mark_dip_triggered)

    # 初始化選股引擎
    selector = SelectionEngine()
    spy_df   = utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")
    sso_df   = utils.load_benchmark(sp_cfg.ETF_DIR / "SSO.csv")
    regime   = MarketRegime(spy_df, sso_df)

    # ── signal_date = 上一個排程調倉日的前一天收盤 ──────────────────────
    # 保證任何日期 sync 都會產生與「上次排程 rebalance」完全一致的結果，
    # 所有帳戶在任何時間 sync 都會看到相同的目標持倉。
    today       = pd.Timestamp.today().normalize()
    rebal_wday  = sp_cfg.REBALANCE_WEEKDAY  # 2 = Wednesday
    days_back   = (today.dayofweek - rebal_wday) % 7
    last_rebal  = today - pd.Timedelta(days=days_back)

    # 資料截斷到 < last_rebal，取最後一筆作為 signal_date
    trimmed_idx = spy_df.index[spy_df.index < last_rebal]
    if len(trimmed_idx) > 0:
        signal_date = trimmed_idx[-1]
        log.info(f"SP500 last_rebal={last_rebal.date()} → signal_date={signal_date.date()}")
    else:
        signal_date = spy_df.index[-1]
        log.warning(f"  SP500: not enough history, using latest {signal_date.date()}")
    sig_date_str = str(signal_date.date())
    log.info(f"SP500 signal date: {sig_date_str}")
    log.info(f"SP500 current IB holdings: {current_sp500_holdings or '(none)'}")

    # ── 讀取持久化狀態 ──────────────────────────────────────────────────
    state = load_state(account_name)
    log.info(f"  state: bull_counter={state['bull_weeks_counter']}  "
             f"dip={state['dip_state']}  last_rebal={state.get('last_rebal_date')}")

    # ── 市場狀態 ────────────────────────────────────────────────────────
    mkt = regime.get_state(signal_date)
    if not mkt:
        log.warning("  SP500: no market regime data")
        return {}, sig_date_str

    spy_close  = mkt["SPY_Close"]
    spy_ma200  = mkt["SPY_MA200"]
    spy_dd     = abs(mkt["SPY_DD"])
    spy_above  = spy_close > spy_ma200

    log.info(f"  SPY=${spy_close:.2f}  MA200=${spy_ma200:.2f}  DD={spy_dd*100:.1f}%  "
             f"{'BULL' if spy_above else 'BEAR'}")

    # ── 牛市計數器更新 ─────────────────────────────────────────────────
    # 每個 signal_date 只計數一次，避免手動 Sync / 重複執行造成計數失真。
    # last_rebal_date 記錄上次計數的日期；同一天重跑不再更新 counter。
    last_counted = state.get("last_rebal_date")
    if last_counted != sig_date_str:
        state = update_bull_counter(state, spy_above, is_rebal_day=True)
        state["last_rebal_date"] = sig_date_str
        log.info(f"  counter updated (new signal_date): "
                 f"bull_counter={state['bull_weeks_counter']}")
    else:
        log.info(f"  counter unchanged (already counted {last_counted}): "
                 f"bull_counter={state['bull_weeks_counter']}")

    bull_confirmed = is_bull_confirmed(state)
    log.info(f"  bull_confirmed={bull_confirmed}")

    # ── 熊市 / 等待分支：bull 未確認 ────────────────────────────────────
    # 計算已觸發的 dip 階層總配置（相對於 SP500 配額）
    ALLOC_MAP = sp_cfg.DIP_BUY_ALLOCATIONS

    if not bull_confirmed:
        dip_ticker = sp_cfg.DIP_BUY_TICKER

        # 如果 SPY < MA200 且啟用 dip-buy，檢查是否要觸發新的階層
        if not spy_above and sp_cfg.ENABLE_BEAR_DIP_BUY:
            for level in (0.15, 0.20, 0.25):
                if spy_dd >= level and not is_dip_triggered(state, level):
                    alloc = ALLOC_MAP[level]
                    log.info(f"  DIP-BUY trigger: -{level*100:.0f}% "
                             f"(actual DD={spy_dd*100:.1f}%) → +{alloc*100:.0f}% 買 {dip_ticker}")
                    state = mark_dip_triggered(state, level)

        # 累積所有已觸發階層的總配置
        total_dip_alloc = 0.0  # 相對於 SP500 配額的比例
        for level, alloc in ALLOC_MAP.items():
            if is_dip_triggered(state, level):
                total_dip_alloc += alloc

        save_state(account_name, state)

        if spy_above:
            log.info(f"  SP500: waiting for bull confirmation (bull_counter < 2)")
        else:
            log.info(f"  SP500: BEAR market → dip_total={total_dip_alloc*100:.0f}%")

        # 清個股，只保留 dip 部位
        if total_dip_alloc > 0:
            log.info(f"  → {dip_ticker} 權重 = {total_dip_alloc*100:.0f}%")
            return {dip_ticker: total_dip_alloc}, sig_date_str
        return {}, sig_date_str

    # ── 牛市已確認：清掉任何 dip 部位 ───────────────────────────────────
    if any(state["dip_state"].values()):
        log.info("  BULL confirmed → reset dip_state")
        state = reset_dip_state(state)

    # ── REBALANCE_WEEKS gate：非 rotation week 只維持現狀 ──────────────
    iso_week = signal_date.isocalendar()[1]
    is_rotation_week = (iso_week % sp_cfg.REBALANCE_WEEKS == 0)
    log.info(f"  iso_week={iso_week}  rotation_week={is_rotation_week} "
             f"(REBALANCE_WEEKS={sp_cfg.REBALANCE_WEEKS})")

    if not is_rotation_week:
        # 非 rotation week：只重新計算現有持股的 ATR 權重
        kept = [t for t in current_sp500_holdings
                if t != sp_cfg.DIP_BUY_TICKER]
        log.info(f"  non-rotation week → re-weight kept holdings: {kept}")
        save_state(account_name, state)
        if not kept:
            return {}, sig_date_str
        metrics = [selector.calculate_metrics(t, signal_date, sp_cfg.LOOKBACK)
                   for t in kept]
        metrics = [m for m in metrics if m and m.get("atr_pct", 0) > 0]
        if not metrics:
            return {}, sig_date_str
        inv_sum = sum(1 / m["atr_pct"] for m in metrics)
        wgt = {m["ticker"]: (1 / m["atr_pct"]) / inv_sum for m in metrics}
        return wgt, sig_date_str

    # ── Rotation week：完整 rotation 流程 ───────────────────────────────
    ranked = selector.scan_market(signal_date, sp_cfg.LOOKBACK)
    top_tickers = [x["ticker"] for x in ranked][:sp_cfg.SELL_RANK_THRESHOLD]

    # 過熱過濾（買入候選用）
    ranked_filtered = [x for x in ranked
                       if x.get("adj_slope", 999) < sp_cfg.MAX_ADJ_SLOPE]
    entry_tickers = [x["ticker"] for x in ranked_filtered]

    log.info(f"  Top {sp_cfg.SELL_RANK_THRESHOLD} tickers: {top_tickers[:10]}...")

    # ── Step 1: 判斷現有持股保留 or 賣出 ─────────────────────────────────
    # 排除 dip 標的（牛市確認時已 reset，但留個保險）
    non_dip_holdings = [t for t in current_sp500_holdings
                        if t != sp_cfg.DIP_BUY_TICKER]
    kept = []
    for ticker in non_dip_holdings:
        reason = None
        if ticker not in top_tickers:
            reason = f"排名跌出前{sp_cfg.SELL_RANK_THRESHOLD}名"
        else:
            m = next((x for x in ranked if x["ticker"] == ticker), None)
            if m and m["price"] < m["exit_ema"]:
                reason = f"跌破EMA{sp_cfg.EXIT_EMA}"

        if reason:
            log.info(f"  SELL {ticker}: {reason}")
        else:
            log.info(f"  KEEP {ticker}: still in top {sp_cfg.SELL_RANK_THRESHOLD}")
            kept.append(ticker)

    # ── Step 2: 補進新股（如果持股不足 TARGET_HOLDINGS）───────────────────
    buy_candidates = []
    needed = sp_cfg.TARGET_HOLDINGS - len(kept)
    if needed > 0:
        candidates = [t for t in entry_tickers if t not in kept][:needed]
        for ticker in candidates:
            m = next((x for x in ranked_filtered if x["ticker"] == ticker), None)
            if m and m.get("max_gap", 0) <= sp_cfg.SKIP_MAX_GAP_PCT:
                log.info(f"  BUY  {ticker}: rank={entry_tickers.index(ticker)+1}"
                         f"  adj_slope={m['adj_slope']:.3f}")
                buy_candidates.append(ticker)

    # ── Step 3: ATR 反比例權重 ───────────────────────────────────────────
    final_tickers = kept + buy_candidates
    if not final_tickers:
        log.info("  SP500: no positions to hold")
        save_state(account_name, state)
        return {}, sig_date_str

    metrics = []
    for t in final_tickers:
        m = next((x for x in ranked if x["ticker"] == t), None) or \
            selector.calculate_metrics(t, signal_date, sp_cfg.LOOKBACK)
        if m and m.get("atr_pct", 0) > 0:
            metrics.append(m)

    if not metrics:
        log.info("  SP500: no valid metrics for weight calculation")
        save_state(account_name, state)
        return {}, sig_date_str

    inv_sum = sum(1 / m["atr_pct"] for m in metrics)
    wgt = {m["ticker"]: (1 / m["atr_pct"]) / inv_sum for m in metrics}

    log.info(f"  SP500 target weights (ATR inverse):")
    for sym, w in sorted(wgt.items(), key=lambda x: -x[1]):
        log.info(f"    {sym}: {w*100:.1f}%")

    save_state(account_name, state)
    return wgt, sig_date_str


def combine_targets(nav: float, sr_wgt: dict, sp500_wgt: dict,
                    prices: dict) -> list:
    """
    合併兩個策略的目標權重，計算每個標的的目標股數。

    SR 持倉：target_shares = NAV × ALLOC_SR × weight / price
    SP500 持倉：target_shares = NAV × ALLOC_SP500 × weight / price

    兩個策略使用不同標的池（ETF vs 個股），不會衝突。
    """
    targets = {}  # symbol -> {'shares': int, 'strategy': str}

    def _valid_price(p) -> bool:
        return p is not None and np.isfinite(p) and p > 0

    # SR 部位（ETF）
    for sym, w in sr_wgt.items():
        price = prices.get(sym, 0)
        if not _valid_price(price):
            log.warning(f"  {sym}: no price, skipping (SR)")
            continue
        shares = int(nav * ALLOC_SR * w / price)
        targets[sym] = {'shares': shares, 'strategy': 'SR',
                        'alloc_w': ALLOC_SR * w}

    # SP500 部位（個股）
    for sym, w in sp500_wgt.items():
        price = prices.get(sym, 0)
        if not _valid_price(price):
            log.warning(f"  {sym}: no price, skipping (SP500)")
            continue
        shares = int(nav * ALLOC_SP500 * w / price)
        if sym in targets:
            # 罕見：同一標的出現在兩個策略中（如 SPY dip-buy）
            targets[sym]['shares'] += shares
            targets[sym]['strategy'] = 'SR+SP500'
            targets[sym]['alloc_w'] += ALLOC_SP500 * w
        else:
            targets[sym] = {'shares': shares, 'strategy': 'SP500',
                            'alloc_w': ALLOC_SP500 * w}

    return targets


# ── Telegram 報告組合 ─────────────────────────────────────────────────────────

def _send_tg_report(nav, order_list, placed, trades, fill_results,
                    pos_detail, prices, sr_wgt, sp500_wgt,
                    sr_signal_date, sp500_signal_date, elapsed, error_msg):
    dry_tag = "[DRY RUN] " if DRY_RUN else ""
    acct_tag = f"[{ACCOUNT_NAME}] " if ACCOUNT_NAME else ""
    status  = "❌ ERROR" if error_msg else "✅ Done"
    lines   = [
        f"<b>{acct_tag}{dry_tag}QuantCore Combo — {status}</b>",
        f"Date: {date.today()}",
        f"SR Signal: {sr_signal_date}  |  SP500 Signal: {sp500_signal_date}",
        f"Alloc: SR {ALLOC_SR:.0%} + SP500 {ALLOC_SP500:.0%} = {ALLOC_SR+ALLOC_SP500:.0%}",
        f"Order: {ORDER_TYPE}  |  Leverage: {LEVERAGE}x",
    ]

    # NAV + 槓桿部位規模
    if nav is not None:
        pos_size = nav * LEVERAGE
        lines.append(f"NAV: <b>${nav:,.2f}</b>  →  部位 <b>${pos_size:,.0f}</b> ({LEVERAGE}x)")

    # SR 目標
    if sr_wgt:
        lines.append(f"\n<b>SR 目標 ({ALLOC_SR:.0%}):</b>")
        for sym, w in sorted(sr_wgt.items(), key=lambda x: -x[1]):
            lines.append(f"  {sym}: {w*100:.0f}%  (→ {ALLOC_SR*w*100:.0f}% of NAV)")

    # SP500 目標
    if sp500_wgt:
        lines.append(f"\n<b>SP500 目標 ({ALLOC_SP500:.0%}):</b>")
        for sym, w in sorted(sp500_wgt.items(), key=lambda x: -x[1]):
            lines.append(f"  {sym}: {w*100:.1f}%  (→ {ALLOC_SP500*w*100:.1f}% of NAV)")

    # 委託結果
    if not placed:
        lines.append("\nNo orders (already on target)")
    elif DRY_RUN:
        lines.append(f"\n<b>委託 DRY RUN ({len(placed)} 筆):</b>")
        for o in placed:
            est_px  = o.get('lmt_price', 0)
            est_val = int(o['qty']) * est_px
            action_tag = "🔴 SELL" if o['action'] == 'SELL' else "🟢 BUY "
            strat = f"[{o.get('strategy', '')}]"
            lines.append(
                f"  {action_tag} {int(o['qty']):4d}sh {o['symbol']}"
                f"  ~${est_px:.2f}/sh  ~${est_val:,.0f}  {strat}"
            )
    else:
        lines.append(f"\n<b>委託結果 ({len(fill_results)} 筆):</b>")
        for f in fill_results:
            icon    = "✅" if f['status'] == 'Filled' else "⚠️"
            filled_val = f['filled'] * f['avg_price']
            px_str  = f"@ ${f['avg_price']:.2f}  ~${filled_val:,.0f}" if f['filled'] > 0 else "未成交"
            action_tag = "🔴 SELL" if f['action'] == 'SELL' else "🟢 BUY "
            lines.append(
                f"  {icon} {action_tag} {f['filled']}/{f['qty']}sh"
                f"  {f['symbol']}  {px_str}  [{f['status']}]"
            )

    # 目前持倉 + 損益
    if pos_detail and prices:
        lines.append("\n<b>目前持倉:</b>")
        total_upnl = 0.0
        for p in sorted(pos_detail, key=lambda x: -abs(x.position * prices.get(x.contract.symbol, 0))):
            sym      = p.contract.symbol
            pos      = int(p.position)
            avg_cost = p.avgCost
            mkt_px   = prices.get(sym, 0)
            upnl     = pos * mkt_px - pos * avg_cost
            pct      = upnl / (pos * avg_cost) * 100 if avg_cost else 0
            total_upnl += upnl
            sign     = "+" if upnl >= 0 else ""
            lines.append(
                f"  {sym}: {pos}sh"
                f"  均${avg_cost:.2f}→現${mkt_px:.2f}"
                f"  {sign}{upnl:.2f}({sign}{pct:.1f}%)"
            )
        sign = "+" if total_upnl >= 0 else ""
        lines.append(f"  <b>未實現損益: {sign}${total_upnl:,.2f}</b>")

    if error_msg:
        lines.append(f"\n<b>Error:</b> {html_mod.escape(str(error_msg)[:300])}")

    lines.append(f"\nElapsed: {elapsed}s")
    tg_send("\n".join(lines))


# ── SR ETF 清單（用於分辨 IB 持倉屬於哪個策略）────────────────────────────────
# 只含 11 支板塊 ETF（與 strategies/sector_rotation/config.py SECTOR_TICKERS 一致）
# SPY / SSO 是 SP500 策略的熊市 dip-buy 標的，歸類為 SP500 持倉
SR_ETFS = {'XLK', 'XLF', 'XLV', 'XLE', 'XLI', 'XLY', 'XLB',
           'XLP', 'XLU', 'XLC', 'XLRE'}


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main(mode: str = "both"):
    """
    mode = "sr"    → 只調 Sector Rotation（週二）
    mode = "sp500" → 只調 SP500 Momentum（週三）
    mode = "both"  → 兩個都調（手動用）
    """
    run_sr    = mode in ("sr", "both")
    run_sp500 = mode in ("sp500", "both")
    mode_label = {"sr": "SR Only", "sp500": "SP500 Only", "both": "Both"}[mode]

    start_ts     = datetime.now()
    nav          = None
    placed       = []
    trades       = []
    fill_results = []
    pos_detail   = []
    prices       = {}
    error_msg    = None
    sr_wgt       = {}
    sp500_wgt    = {}
    sr_signal_date    = "N/A"
    sp500_signal_date = "N/A"

    log.info(f"{'='*60}")
    log.info(f"  QuantCore Rebalance  {date.today()}  [{mode_label}]")
    log.info(f"  dry_run={DRY_RUN}  order_type={ORDER_TYPE}")
    log.info(f"  Alloc: SR {ALLOC_SR:.0%} + SP500 {ALLOC_SP500:.0%} = {ALLOC_SR+ALLOC_SP500:.0%}")
    log.info(f"{'='*60}")

    try:
        # ── 1. 計算 SR 目標權重（不需 IB）─────────────────────────────────────
        if run_sr:
            log.info("Step 1: Computing Sector Rotation target weights...")
            sr_wgt, sr_signal_date = compute_sr_weights()
        else:
            log.info("Step 1: SR skipped (sp500 mode)")

        # ── 2. 連線 IB ───────────────────────────────────────────────────────
        log.info("Step 2: Connecting to IB Gateway...")
        ib = connect_ib()

    except Exception as e:
        error_msg = str(e)
        log.error(f"ERROR in signal / IB connect: {e}", exc_info=True)
        elapsed = (datetime.now() - start_ts).seconds
        _send_tg_report(nav, [], placed, trades, fill_results,
                        pos_detail, prices, sr_wgt, sp500_wgt,
                        sr_signal_date, sp500_signal_date, elapsed, error_msg)
        raise

    try:
        # ── 3. 帳戶資訊 + 辨別持倉歸屬 ──────────────────────────────────────
        nav = get_nav(ib)
        log.info(f"Step 3: NAV = ${nav:,.2f}  →  Position Size = ${nav*LEVERAGE:,.0f}")

        current_pos = get_positions(ib)
        log.info(f"Current positions: {current_pos if current_pos else '(none)'}")

        # 載入 SP500 成分股清單（用於分辨持倉歸屬）
        from strategies.sp500_momentum.selection import SelectionEngine
        from strategies.sp500_momentum import config as sp_cfg, utils
        _selector = SelectionEngine()
        _spy_df = utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")
        _sp500_universe = set(_selector.get_constituents(_spy_df.index[-1]))
        # SPY/SSO 也歸為 SP500 策略的熊市 dip-buy 標的
        _sp500_universe |= {'SPY', 'SSO'}

        # 分辨當前持股：SR ETFs vs SP500 個股 vs 其他（手動操作，排除在外）
        current_sr_holdings = [sym for sym in current_pos if sym in SR_ETFS]
        current_sp500_holdings = [sym for sym in current_pos
                                  if sym not in SR_ETFS and sym in _sp500_universe]
        current_other_holdings = [sym for sym in current_pos
                                  if sym not in SR_ETFS and sym not in _sp500_universe]
        log.info(f"  SR holdings:    {current_sr_holdings or '(none)'}")
        log.info(f"  SP500 holdings: {current_sp500_holdings or '(none)'}")
        if current_other_holdings:
            other_val = sum(abs(current_pos[s]) * 1 for s in current_other_holdings)
            log.info(f"  OTHER holdings: {current_other_holdings} (excluded from strategy)")
            log.info(f"    -> These are manual/discretionary trades, NOT rebalanced")

        # ── 4. 計算 SP500 目標權重（基於 IB 實際持倉 + rank buffer）────────
        if run_sp500:
            log.info("Step 4: Computing SP500 Momentum weights (live, rank-aware)...")
            sp500_wgt, sp500_signal_date = compute_sp500_weights(
                current_sp500_holdings, ACCOUNT_NAME)
        else:
            log.info("Step 4: SP500 skipped (sr mode)")
            # SR only 模式：保留 SP500 現有持倉不動
            for sym in current_sp500_holdings:
                sp500_wgt[sym] = 0  # 佔位用，不會產生 order

        # ── 5. 即時報價 ─────────────────────────────────────────────────────
        all_syms = list(set(
            list(sr_wgt.keys()) +
            list(sp500_wgt.keys()) +
            list(current_pos.keys())
        ))
        log.info(f"Step 5: Getting prices for {len(all_syms)} symbols...")
        prices = get_prices(ib, all_syms)

        # ── 6. 合併目標 + 計算差額 ──────────────────────────────────────────
        log.info("Step 6: Computing targets and deltas...")

        # 依 mode 決定哪些標的要計算目標、哪些保持不動
        if mode == "both":
            targets = combine_targets(nav, sr_wgt, sp500_wgt, prices)
        elif mode == "sr":
            # 只算 SR 目標，SP500 持倉維持現狀
            targets = combine_targets(nav, sr_wgt, {}, prices)
            # SP500 持倉標記為「保持現狀」→ target = 現有股數
            for sym in current_sp500_holdings:
                targets[sym] = {'shares': int(current_pos[sym]),
                                'strategy': 'SP500 (hold)', 'alloc_w': 0}
        elif mode == "sp500":
            # 只算 SP500 目標，SR 持倉維持現狀
            targets = combine_targets(nav, {}, sp500_wgt, prices)
            # SR 持倉標記為「保持現狀」→ target = 現有股數
            for sym in current_sr_holdings:
                targets[sym] = {'shares': int(current_pos[sym]),
                                'strategy': 'SR (hold)', 'alloc_w': 0}

        order_list = []

        # 處理所有策略標的（排除 OTHER 手動持倉，不會被賣出或調整）
        # 加入 REBALANCE_THRESHOLD 過濾 — 小於 3% NAV 漂移不動作（與 backtest 一致）
        from strategies.sp500_momentum import config as sp_cfg
        rebal_thresh = sp_cfg.REBALANCE_THRESHOLD  # 0.03

        all_target_syms = set(targets.keys()) | set(current_pos.keys())
        all_target_syms -= set(current_other_holdings)  # 排除手動操作標的
        for sym in all_target_syms:
            price = prices.get(sym, 0)
            if price <= 0:
                log.warning(f"  {sym}: no price, skipping")
                continue

            target_shares = targets.get(sym, {}).get('shares', 0)
            curr_shares   = int(current_pos.get(sym, 0))
            delta         = target_shares - curr_shares
            strategy      = targets.get(sym, {}).get('strategy', 'EXIT')

            # REBALANCE_THRESHOLD 過濾：小於 3% NAV 漂移不動作
            # 例外：完全清倉（target=0）或從無到有建倉（current=0）必須執行
            if delta != 0 and target_shares > 0 and curr_shares > 0:
                drift_val = abs(delta) * price
                drift_pct = drift_val / nav if nav > 0 else 0
                if drift_pct < rebal_thresh:
                    log.info(
                        f"  {sym} [{strategy}]: target={target_shares:5d}sh "
                        f"current={curr_shares:5d}sh  delta={delta:+d}  "
                        f"drift={drift_pct*100:.1f}% < {rebal_thresh*100:.0f}% → skip"
                    )
                    continue

            log.info(
                f"  {sym} [{strategy}]: target={target_shares:5d}sh "
                f"current={curr_shares:5d}sh  delta={delta:+d}"
            )

            if delta != 0:
                order_list.append({
                    'symbol':    sym,
                    'action':    'BUY' if delta > 0 else 'SELL',
                    'qty':       abs(delta),
                    'lmt_price': round(price * (0.9995 if delta > 0 else 1.0005), 2),
                    'strategy':  strategy,
                })

        # ── 7. 下單（先取消既有掛單，避免重複）─────────────────────────
        if not order_list:
            log.info("Step 7: No changes needed.")
        else:
            # 訂單去重
            seen_syms = set()
            dedup_list = []
            for o in order_list:
                if o['symbol'] in seen_syms:
                    log.warning(f"  duplicate order for {o['symbol']}, keeping first")
                    continue
                seen_syms.add(o['symbol'])
                dedup_list.append(o)
            order_list = dedup_list

            # 取消該帳戶既有掛單
            if not DRY_RUN:
                open_trades = ib.openTrades()
                if open_trades:
                    log.info(f"  Cancelling {len(open_trades)} existing open orders...")
                    for t in open_trades:
                        ib.cancelOrder(t.order)
                    ib.sleep(2)

            log.info(f"Step 7: Placing {len(order_list)} orders ({ORDER_TYPE})...")
            placed, trades = place_orders(ib, order_list)

        # ── 8. 確認成交 ─────────────────────────────────────────────────────
        if trades:
            log.info("Step 8: Checking fills...")
            fill_results = check_fills(trades)

        # ── 9. 帳戶現況 + 損益 ──────────────────────────────────────────────
        log.info("Step 9: Fetching positions detail for P&L...")
        pos_detail = get_positions_detail(ib)

        log.info(f"{'='*60}")
        log.info(f"  Rebalance Done  [{mode_label}]")
        log.info(f"{'='*60}")

    except Exception as e:
        error_msg = str(e)
        log.error(f"ERROR: {e}", exc_info=True)
        raise

    finally:
        ib.disconnect()
        log.info("Disconnected from IB Gateway.")

        # ── Excel 交易記錄 ────────────────────────────────────────────────
        if placed:
            signal_str = f"SR:{sr_signal_date} SP500:{sp500_signal_date}"
            for o in placed:
                strat = o.get('strategy', 'Unknown')
                append_trade_log([o], fill_results, signal_str,
                                 ORDER_TYPE, LEVERAGE, DRY_RUN,
                                 strategy=strat, account_name=ACCOUNT_NAME)
            log.info("Trade log written -> trade_log.xlsx")

        elapsed = (datetime.now() - start_ts).seconds
        _send_tg_report(
            nav=nav,
            order_list=order_list if 'order_list' in dir() else [],
            placed=placed,
            trades=trades,
            fill_results=fill_results,
            pos_detail=pos_detail,
            prices=prices,
            sr_wgt=sr_wgt,
            sp500_wgt=sp500_wgt,
            sr_signal_date=sr_signal_date,
            sp500_signal_date=sp500_signal_date,
            elapsed=elapsed,
            error_msg=error_msg,
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="QuantCore Rebalance")
    parser.add_argument("--mode", choices=["sr", "sp500", "both"],
                        default="both",
                        help="sr=Sector Rotation only (Tue), "
                             "sp500=SP500 Momentum only (Wed), "
                             "both=all (default)")
    parser.add_argument("--order-type", choices=["MKT", "MOO", "LMT"],
                        default=None,
                        help="Override order type from config (e.g. MKT for manual intraday)")
    parser.add_argument("--account", default=None,
                        help="Account name from accounts.json (e.g. A, B), "
                             "or 'all' to run all accounts sequentially")
    args = parser.parse_args()

    if args.account:
        from account_utils import get_account_config, get_all_account_configs
        accounts = (get_all_account_configs() if args.account.lower() == 'all'
                    else [get_account_config(args.account)])
        for acfg in accounts:
            _apply_config(acfg)
            if args.order_type:
                ORDER_TYPE = args.order_type
            log.info(f">>> Account: {ACCOUNT_NAME} ({ACCOUNT}) port={PORT} <<<")
            main(mode=args.mode)
    else:
        # 向下相容：不帶 --account 時使用 config.json 原設定
        if args.order_type:
            ORDER_TYPE = args.order_type
        main(mode=args.mode)
