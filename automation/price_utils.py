"""
QuantCore — 穩健的價格取得工具

提供 4 層 fallback 機制取得報價：
  1. 本 IB 連線即時報價 (midpoint / last / close / delayed*)
  2. 外部 PRICE_FEED_PORT 連線（e.g. Rosa 7497 有訂閱即時資料）
  3. 同連線改用 delayed 報價
  4. 本地 CSV 最後收盤價

用途：多帳戶共用同一份 market data 訂閱，新帳戶無需單獨訂閱。
"""

import numpy as np


def _ok(x) -> bool:
    return x is not None and np.isfinite(x) and x > 0


def _safe_price(t) -> float:
    """從 ticker 逐級取得有效價格，全部無效回傳 0。"""
    mid = t.midpoint()
    if _ok(mid):
        return float(mid)
    if _ok(t.last):
        return float(t.last)
    if _ok(t.close):
        return float(t.close)
    for attr in ('delayedLast', 'delayedClose', 'delayedBid', 'delayedAsk'):
        v = getattr(t, attr, None)
        if _ok(v):
            return float(v)
    return 0.0


def load_csv_close(symbol: str) -> float:
    """從本地 CSV 取最後一筆收盤價，查不到回傳 0。"""
    try:
        import pandas as pd
        from strategies.sp500_momentum import config as sp_cfg
        from strategies.sp500_momentum.utils import stock_csv_path

        # ETF filenames are already their canonical symbols.  Stock files use
        # the provider naming contract (BRK.B -> BRK-B), including the class
        # shares opened by issue #52.
        paths = (
            sp_cfg.ETF_DIR / f"{symbol}.csv",
            stock_csv_path(sp_cfg.STOCK_DIR, symbol),
        )
        for f in paths:
            if f.exists():
                df = pd.read_csv(f, index_col=0, parse_dates=True)
                c = float(df['Close'].dropna().iloc[-1])
                if _ok(c):
                    return c
    except Exception:
        pass
    return 0.0


def robust_get_prices(ib, symbols: list,
                      host: str = '127.0.0.1',
                      price_feed_port: int | None = 4002,
                      verbose_log=None, *, feed_client_id: int = 99,
                      readonly: bool = False) -> dict:
    """
    多層 fallback 取得 symbols 的報價，回傳 {sym: price} 或 0（全部失敗時）。

    Parameters
    ----------
    ib : ib_insync.IB
        已連線的 IB 實例（本帳戶）
    symbols : list[str]
    host : str
    price_feed_port : int, 預設 4002 (Rosa)
        若本帳戶無即時訂閱，會在此 port 另開連線抓價
        設為 None 或與 ib.client.port 相同則跳過此步
    verbose_log : callable(str) or None
        若提供，會對每個取到價格的 symbol 呼叫一次

    Returns
    -------
    dict: {sym: price (float > 0)} 或 {sym: 0} 如果全部 fallback 都失敗
    """
    from types import SimpleNamespace

    from broker import Broker
    from symbol_contract import canonical_symbol, ib_stock

    def _log(msg):
        if verbose_log:
            verbose_log(msg)

    if not symbols:
        return {}

    # Step 1: 本連線即時
    canonical_symbols = [canonical_symbol(s) for s in symbols]
    contracts = [ib_stock(s) for s in canonical_symbols]
    ib.qualifyContracts(*contracts)
    tickers = ib.reqTickers(*contracts)

    prices = {}
    need_feed = []
    for t in tickers:
        sym = canonical_symbol(t.contract.symbol)
        p = _safe_price(t)
        if p > 0:
            prices[sym] = p
            _log(f"  {sym}: ${p:.2f}")
        else:
            need_feed.append(sym)
            prices[sym] = 0.0

    # Step 2: 透過 PRICE_FEED_PORT 另開連線
    if need_feed and price_feed_port and price_feed_port != ib.client.port:
        _log(f"  {len(need_feed)} symbols no live price, trying price feed port {price_feed_port}...")
        feed = None
        try:
            # 經 Broker 連報價 feed port（#185）：只為借報價，不指定帳戶
            feed = Broker().connect(
                SimpleNamespace(host=host, port=price_feed_port, client_id=feed_client_id,
                                account=""), timeout=10, readonly=readonly)
            feed_ib = feed.ib
            feed_contracts = [ib_stock(s) for s in need_feed]
            feed_ib.qualifyContracts(*feed_contracts)
            feed_tickers = feed_ib.reqTickers(*feed_contracts)
            recovered = []
            for t in feed_tickers:
                sym = canonical_symbol(t.contract.symbol)
                p = _safe_price(t)
                if p > 0:
                    prices[sym] = p
                    recovered.append(sym)
                    _log(f"  {sym}: ${p:.2f} (feed)")
            need_feed = [s for s in need_feed if s not in recovered]
        except Exception as e:
            _log(f"  price feed connect failed: {e}")
        finally:
            if feed is not None:
                feed.close()

    # Step 3: 本連線 delayed 報價
    if need_feed:
        _log(f"  {len(need_feed)} still missing, trying delayed on own port...")
        try:
            ib.reqMarketDataType(3)  # 3 = delayed
            delayed_contracts = [ib_stock(s) for s in need_feed]
            ib.qualifyContracts(*delayed_contracts)
            delayed_tickers = ib.reqTickers(*delayed_contracts)
            for t in delayed_tickers:
                sym = canonical_symbol(t.contract.symbol)
                p = _safe_price(t)
                if p > 0:
                    prices[sym] = p
                    _log(f"  {sym}: ${p:.2f} (delayed)")
        except Exception as e:
            _log(f"  delayed data fetch failed: {e}")
        finally:
            try:
                ib.reqMarketDataType(1)
            except Exception:
                pass

    # Step 4: 本地 CSV 最後收盤
    for sym in canonical_symbols:
        if prices.get(sym, 0) <= 0:
            c = load_csv_close(sym)
            if c > 0:
                prices[sym] = c
                _log(f"  {sym}: ${c:.2f} (CSV close)")
            else:
                prices[sym] = 0.0
                _log(f"  {sym}: NO PRICE")

    return prices
