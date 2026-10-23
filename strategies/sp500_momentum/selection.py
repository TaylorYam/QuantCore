"""
SP500 Momentum — 選股引擎
掃描 SP500 成分股，依 Adjusted Slope 排名。
"""

import os
import pandas as pd
import numpy as np
import scipy.stats as stats

from data import market_data

from . import config
from . import utils
from .constituents import load_constituents


class SelectionEngine:
    def __init__(self, data_cache: dict = None,
                 use_constituents_cache: bool | None = None,
                 constituents_cache_dir=None):
        self.data_cache    = data_cache or {}
        self._unusable: set = set()      # 缺檔或別名界限外：同一輪不再重讀、不再重記 log
        self.scan_cache    = {}     # {(date, lookback): sorted_list}
        self.metrics_cache = {}     # {(ticker, date, lookback, ema, atr): dict}
        self.use_constituents_cache = use_constituents_cache
        self.constituents_cache_dir = constituents_cache_dir
        self.constituents_df = self._load_constituents()
        self._all_tickers_loaded = False

    # ── 成分股 ────────────────────────────────────────────────────────────────

    def _load_constituents(self) -> pd.DataFrame:
        path = config.REF_DIR / config.CONST_FILE
        print(f"Loading constituents from {path}...")
        try:
            return load_constituents(
                path,
                use_cache=self.use_constituents_cache,
                cache_dir=self.constituents_cache_dir,
            )
        except Exception as e:
            print(f"Error loading constituents: {e}")
            return pd.DataFrame()

    def get_constituents(self, date: pd.Timestamp) -> list[str]:
        """回傳指定日期的成分股清單（已套用黑名單與類股排除）。"""
        if self.constituents_df.empty:
            return []

        if date not in self.constituents_df.index:
            loc = self.constituents_df.index.get_indexer([date], method="pad")[0]
            if loc == -1:
                return []
            target = self.constituents_df.index[loc]
        else:
            target = date

        row = self.constituents_df.loc[target]
        tickers = [t.replace(".txt", "").strip()
                   for t in row.values if isinstance(t, str)]

        # 套用黑名單
        for bl_date_str, bl_ticker in config.BLACKLIST:
            bl_date = pd.to_datetime(bl_date_str)
            if date > bl_date and bl_ticker.upper() in tickers:
                tickers.remove(bl_ticker.upper())

        # 類股是否進選股由 config 控制；下單代碼轉換在 IB adapter（#52）。
        if not getattr(config, "CLASS_SHARES_TRADABLE", True):
            tickers = [t for t in tickers if "." not in t]

        return tickers

    # ── 資料載入 ──────────────────────────────────────────────────────────────

    PRECOMPUTED_EMA_PERIODS = [20, 30, 40, 50, 60]

    def _get_ticker_data(self, ticker: str, as_of=None) -> pd.DataFrame | None:
        """從 cache 或 disk 讀取股票資料，並預計算常用 EMA。

        A historical alias is resolved with the signal session.  The source
        frame is clipped to the alias window before caching, so a later
        session cannot accidentally reuse the successor's bars outside the
        reviewed identity interval.
        """
        source = market_data.source_symbol(ticker, as_of)
        cache_key = ticker if source == market_data.provider_symbol(ticker) else (ticker, source)
        if ticker in self.data_cache and as_of is None:
            return self.data_cache[ticker]
        if cache_key in self.data_cache:
            return self.data_cache[cache_key]
        if cache_key in self._unusable:
            return None

        path = market_data.csv_path(config.STOCK_DIR, ticker, as_of)
        df = utils.load_stock(path)

        if df is not None and not df.empty:
            df = market_data.alias_window(df, ticker, as_of)
            if df.empty:
                market_data.report(path, market_data.OUTSIDE_ALIAS)
                self._unusable.add(cache_key)
                return None
            # 優先用 Adj Close 計算 EMA（與原始系統一致）
            price_col = "Adj Close" if "Adj Close" in df.columns else "Close"
            for period in self.PRECOMPUTED_EMA_PERIODS:
                df[f"_EMA{period}"] = df[price_col].ewm(span=period, adjust=False).mean()
            self.data_cache[cache_key] = df
            return df

        self._unusable.add(cache_key)
        return None

    def preload_all_data(self, start_date=None, end_date=None):
        """預載入所有成分股到記憶體，回測前呼叫可大幅提速。"""
        if self._all_tickers_loaded:
            return
        if self.constituents_df.empty:
            return

        all_tickers = set()
        subset = self.constituents_df
        if start_date and end_date:
            mask = (subset.index >= pd.to_datetime(start_date)) & \
                   (subset.index <= pd.to_datetime(end_date))
            subset = subset[mask]

        for row in subset.itertuples(index=False):
            for t in row:
                if isinstance(t, str):
                    all_tickers.add(t.replace(".txt", "").strip())

        print(f"Preloading {len(all_tickers)} tickers into memory...")
        loaded = sum(1 for t in all_tickers if self._get_ticker_data(t) is not None)
        print(f"Loaded {loaded} tickers successfully.")
        self._all_tickers_loaded = True

    # ── 指標計算 ──────────────────────────────────────────────────────────────

    def calculate_metrics(self, ticker: str, current_date: pd.Timestamp,
                           lookback: int = None) -> dict | None:
        """計算選股指標：adj_slope, max_gap, price, exit_ema, atr, atr_pct。"""
        if lookback is None:
            lookback = config.LOOKBACK

        exit_ema_period = config.EXIT_EMA
        atr_period      = config.ATR_PERIOD
        cache_key = (ticker, current_date, lookback, exit_ema_period, atr_period)

        if cache_key in self.metrics_cache:
            return self.metrics_cache[cache_key]

        df = self._get_ticker_data(ticker, current_date)
        if df is None or df.empty:
            return None

        hist = df[df.index <= current_date].tail(lookback + exit_ema_period)
        if len(hist) < lookback:
            return None

        chunk  = hist.iloc[-lookback:]
        closes = chunk["Close"].values
        opens  = chunk["Open"].values
        highs  = chunk["High"].values
        lows   = chunk["Low"].values
        # 訊號計算優先使用 Adj Close（與原始系統一致）
        trend  = chunk["Adj Close"].values if "Adj Close" in chunk.columns else closes

        # 1. Adjusted Slope
        try:
            y_log = np.log(trend)
            x     = np.arange(len(y_log))
            slope, _, r, _, _ = stats.linregress(x, y_log)
            adj_slope = ((1 + slope) ** lookback - 1) * (r ** 2)
        except Exception:
            adj_slope = -999

        # 2. Max Gap（跳空缺口）
        prev_closes  = np.roll(closes, 1); prev_closes[0] = opens[0]
        gaps         = np.abs((opens - prev_closes) / prev_closes)
        max_gap      = float(np.max(gaps[1:]))

        # 3. Exit EMA
        ema_col = f"_EMA{exit_ema_period}"
        if ema_col in hist.columns:
            exit_ema = float(hist[ema_col].iloc[-1])
        else:
            exit_ema = float(
                hist["Close"].ewm(span=exit_ema_period, adjust=False).mean().iloc[-1]
            )

        # 4. ATR
        prev_c = np.roll(closes, 1); prev_c[0] = closes[0]
        tr     = np.maximum(highs - lows,
                            np.maximum(np.abs(highs - prev_c),
                                       np.abs(lows  - prev_c)))
        atr    = float(np.mean(tr[-atr_period:]))
        price  = float(closes[-1])
        atr_pct = atr / price if price > 0 else 0.0

        # 5. 噴出因子 kurt60（ADR-0011）：日報酬 rolling kurtosis。
        #    與研究同源定義（robustness/takeprofit_common.blowoff_factors）：
        #    Adj Close 優先、pct_change 後 dropna 取尾 60、樣本不足為 NaN
        #    （NaN = 資料不足不封鎖）。
        kurt_w = getattr(config, "BLOWOFF_KURT_WINDOW", 60)
        kurt_col = "Adj Close" if "Adj Close" in hist.columns else "Close"
        kurt_rets = hist[kurt_col].pct_change().dropna().tail(kurt_w)
        kurt60 = float(kurt_rets.kurt()) if len(kurt_rets) == kurt_w else float("nan")

        result = {
            "ticker":    ticker,
            "adj_slope": adj_slope,
            "max_gap":   max_gap,
            "price":     price,
            "exit_ema":  exit_ema,
            "atr":       atr,
            "atr_pct":   atr_pct,
            "kurt60":    kurt60,
        }
        self.metrics_cache[cache_key] = result
        return result

    # ── 市場掃描 ──────────────────────────────────────────────────────────────

    def scan_market(self, date: pd.Timestamp, lookback: int = None) -> list[dict]:
        """掃描成分股，依 adj_slope 排名，回傳 list[dict]。"""
        if lookback is None:
            lookback = config.LOOKBACK

        cache_key = (date, lookback)
        if cache_key in self.scan_cache:
            return self.scan_cache[cache_key]

        tickers = self.get_constituents(date)
        if not tickers:
            return []

        results = [m for t in tickers
                   if (m := self.calculate_metrics(t, date, lookback)) is not None]

        # 跳空缺口過濾
        filtered = [r for r in results if r["max_gap"] <= config.SKIP_MAX_GAP_PCT]

        # 依 adj_slope 降序排名
        ranked = sorted(filtered, key=lambda x: x["adj_slope"], reverse=True)

        self.scan_cache[cache_key] = ranked
        return ranked
