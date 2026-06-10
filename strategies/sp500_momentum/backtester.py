"""
SP500 Momentum — 回測引擎
ATR 反比例權重 + 每週風險再平衡 + 熊市 SSO 抄底
"""

import os
import json
import pandas as pd
import numpy as np

from . import config
from .selection    import SelectionEngine
from .market_regime import MarketRegime
from . import utils


class SP500Backtester:

    def __init__(self,
                 start_date:     str  = None,
                 end_date:       str  = None,
                 initial_capital: float = None,
                 compounding:    bool = True,
                 selector:       SelectionEngine = None,
                 spy_df:         pd.DataFrame = None,
                 sso_df:         pd.DataFrame = None,
                 write_reports:  bool = False,
                 dip_ticker_map: dict = None):

        self.initial_capital = initial_capital or config.INITIAL_CASH
        self.cash            = self.initial_capital
        self.holdings        = {}   # {ticker: qty}
        self.avg_costs       = {}   # {ticker: avg_cost_per_share}
        self.target_weights  = {}   # {ticker: target_weight}
        self.history         = []   # [{'Date', 'Equity', 'Cash'}]
        self.trades          = []

        self.compounding     = compounding
        self.write_reports   = write_reports

        self._start_date_raw = start_date or config.START_DATE
        self._end_date_raw   = end_date   or config.END_DATE

        # 熊市狀態
        self.dip_state         = {0.15: False, 0.20: False, 0.25: False}
        self.bull_weeks_counter = 0
        self.rebalance_snapshots = []

        # dip_ticker_map: {dip_level: ticker}，預設全用 config.DIP_BUY_TICKER
        self.dip_ticker_map = dip_ticker_map or {
            0.15: config.DIP_BUY_TICKER,
            0.20: config.DIP_BUY_TICKER,
            0.25: config.DIP_BUY_TICKER,
        }
        # 所有可能的 dip 標的（用於 _is_dip 判斷）
        self._dip_tickers: set = set(self.dip_ticker_map.values())

        # 預載入 benchmark（外部傳入或自行讀取）
        self._preloaded_spy = spy_df
        self._preloaded_sso = sso_df

        self.selector = selector or SelectionEngine()

        self._prep()

    # ── 初始化 ────────────────────────────────────────────────────────────────

    def _prep(self):
        # 讀取 benchmark
        self.spy_df = (self._preloaded_spy if self._preloaded_spy is not None
                       else utils.load_benchmark(config.ETF_DIR / "SPY.csv"))
        self.sso_df = (self._preloaded_sso if self._preloaded_sso is not None
                       else utils.load_benchmark(config.ETF_DIR / "SSO.csv"))

        self.market_regime = MarketRegime(self.spy_df, self.sso_df)

        self.start_date = (pd.to_datetime(self._start_date_raw)
                           if self._start_date_raw
                           else self.market_regime.spy.index[200])
        self.end_date   = (pd.to_datetime(self._end_date_raw)
                           if self._end_date_raw
                           else self.market_regime.spy.index[-1])

        # 預載成分股資料
        self.selector.preload_all_data(self.start_date, self.end_date)

        # 以 SPY 為交易日曆
        self.calendar = self.spy_df.index

    # ── 主回測迴圈 ────────────────────────────────────────────────────────────

    def run(self):
        trading_days = self.calendar[
            (self.calendar >= self.start_date) &
            (self.calendar <= self.end_date)
        ]

        self.dip_state = {0.15: False, 0.20: False, 0.25: False}
        total = len(trading_days)

        for i, date in enumerate(trading_days):
            if self.write_reports and (i % 500 == 0 or i == total - 1):
                pct = int((i + 1) / total * 100)
                bar = "*" * (pct // 5) + " " * (20 - pct // 5)
                print(f"\r[{bar}{pct:3d}%] {i+1} of {total} completed", end="", flush=True)

            self._update_equity(date)
            self._check_bear_sso_logic(date)

            iso_week          = date.isocalendar()[1]
            is_rebalance_day  = (date.weekday() == config.REBALANCE_WEEKDAY)
            is_rotation_week  = (iso_week % config.REBALANCE_WEEKS == 0)

            if is_rebalance_day:
                prev_idx    = i - 1
                signal_date = trading_days[prev_idx] if prev_idx >= 0 else date
                self._unified_rebalance(date, signal_date, is_rotation_week)

        if self.write_reports:
            print()  # newline after progress bar

        if not config.LIVE_MODE:
            self._force_close_all(self.end_date)
        else:
            self._update_equity(self.end_date)

        return self

    # ── 核心再平衡 ────────────────────────────────────────────────────────────

    def _unified_rebalance(self, date, signal_date, is_rotation_week):
        if self.bull_weeks_counter < 2:
            stocks = [t for t in self.holdings if not self._is_dip(t)]
            if stocks:
                self._close_positions(date, "STOCK")
                self.target_weights.clear()
            return

        # Step 1: 輪動賣出
        rotation_sells = set()
        if is_rotation_week:
            for ticker, qty, price, reason in self._get_rotation_sells(date, signal_date):
                rotation_sells.add(ticker)
                eq = self._get_total_equity(date)
                wb = price * self.holdings.get(ticker, 0) / eq * 100 if eq > 0 else 0
                if self.holdings.get(ticker, 0) >= qty:
                    self._sell(ticker, date, price, qty, reason, weight_before=wb)
                    self.target_weights.pop(ticker, None)

        # Step 2: 確定買入候選 + ATR 權重
        buy_candidates, full_weights = [], {}
        if is_rotation_week:
            buy_candidates, full_weights = self._get_buy_candidates(
                date, signal_date, rotation_sells)

        if not full_weights:
            kept = [t for t in self.holdings
                    if not self._is_dip(t) and t not in rotation_sells]
            if kept:
                m_list = [self.selector.calculate_metrics(t, date, config.LOOKBACK)
                          for t in kept]
                full_weights = self._calc_atr_weights(
                    [m for m in m_list if m and m.get("atr_pct", 0) > 0])

        if full_weights:
            self.target_weights = full_weights.copy()

        # Step 3: 超重賣出
        for ticker, qty, price, reason, wb in self._get_overweight_sells(
                date, rotation_sells, full_weights):
            if self.holdings.get(ticker, 0) >= qty:
                self._sell(ticker, date, price, qty, reason, weight_before=wb)

        # Step 4: 買入
        newly_bought = set()
        if is_rotation_week and buy_candidates:
            eq = self._get_total_equity(date) if self.compounding else self.initial_capital
            for ticker, exec_price in buy_candidates:
                w = full_weights.get(ticker, 0)
                if w <= 0 or exec_price <= 0:
                    continue
                alloc  = eq * w
                amount = min(alloc, self.cash)
                qty    = int(amount / (exec_price * (1 + config.COMMISSION)))
                if qty > 0 and self.cash >= exec_price * qty * (1 + config.COMMISSION):
                    self._buy(ticker, date, exec_price, qty,
                              f"ATR Buy (W:{w*100:.1f}%)", target_weight=w)
                    newly_bought.add(ticker)

        # Step 5: 剩餘現金補倉
        self._distribute_remaining_cash(date, newly_bought)

        if is_rotation_week:
            self._record_snapshot(date, signal_date)

    # ── 選股輔助 ──────────────────────────────────────────────────────────────

    def _get_rotation_sells(self, date, signal_date):
        ranked        = self.selector.scan_market(signal_date, config.LOOKBACK)
        top_tickers   = [x["ticker"] for x in ranked][:config.SELL_RANK_THRESHOLD]
        current       = [t for t in self.holdings
                         if not self._is_dip(t) and t != "SPY"]
        sells = []
        for ticker in current:
            reason = None
            if ticker not in top_tickers:
                reason = f"排名跌出前{config.SELL_RANK_THRESHOLD}名"
            else:
                m = next((x for x in ranked if x["ticker"] == ticker), None)
                if m and m["price"] < m["exit_ema"]:
                    reason = f"跌破EMA{config.EXIT_EMA}"
            if reason:
                price = self._get_price(ticker, date, use_open=True)
                if price > 0:
                    sells.append((ticker, self.holdings[ticker], price, reason))
        return sells

    def _get_buy_candidates(self, date, signal_date, rotation_sells):
        ranked = self.selector.scan_market(signal_date, config.LOOKBACK)

        # 過熱過濾
        if config.MAX_ADJ_SLOPE is not None:
            ranked = [x for x in ranked if x.get("adj_slope", 999) < config.MAX_ADJ_SLOPE]

        kept = [t for t in self.holdings
                if not self._is_dip(t) and t not in rotation_sells]
        needed = config.TARGET_HOLDINGS - len(kept)

        # ENTRY_EMA_FILTER（預設 False，ADR-0005）：WF 否決進場 EMA 過濾 —
        # rank 優先；高排名股同輪 EMA 賣出後買回（=續抱）是刻意行為。
        # 與 signals.py 讀同一 flag，live == backtest。
        buy_candidates = []
        if needed > 0:
            if getattr(config, "ENTRY_EMA_FILTER", False):
                eligible = utils.filter_entry_eligible(ranked, exclude=set(kept))
            else:
                eligible = [x for x in ranked if x.get("ticker") not in kept]
            for x in eligible[:needed]:
                ticker = x["ticker"]
                price = self._get_price(ticker, date, use_open=True)
                if price > 0:
                    buy_candidates.append((ticker, price))

        all_tickers = kept + [t for t, _ in buy_candidates]
        metrics = []
        for t in all_tickers:
            m = next((x for x in ranked if x["ticker"] == t), None) or \
                self.selector.calculate_metrics(t, date, config.LOOKBACK)
            if m and m.get("atr_pct", 0) > 0:
                metrics.append(m)

        full_weights = self._calc_atr_weights(metrics)
        return buy_candidates, full_weights

    def _get_overweight_sells(self, date, exclude, full_weights):
        sells = []
        eq    = self._get_total_equity(date)
        if eq <= 0:
            return sells
        current = [t for t in self.holdings
                   if not self._is_dip(t) and t not in exclude]
        for ticker in current:
            if ticker not in full_weights:
                continue
            target_w = full_weights[ticker]
            qty      = self.holdings.get(ticker, 0)
            price    = self._get_price(ticker, date, use_open=True)
            if price <= 0:
                continue
            current_w = price * qty / eq
            if current_w - target_w >= config.REBALANCE_THRESHOLD:
                diff_val  = (current_w - target_w) * eq
                qty_sell  = min(int(diff_val / price), qty)
                if qty_sell > 0:
                    wb     = current_w * 100
                    reason = f"超重賣出 ({current_w*100:.1f}%→{target_w*100:.1f}%)"
                    sells.append((ticker, qty_sell, price, reason, wb))
        return sells

    def _distribute_remaining_cash(self, date, exclude):
        current = [t for t in self.holdings
                   if not self._is_dip(t) and t not in exclude]
        if not current or self.cash <= 0:
            return
        eq = self._get_total_equity(date)
        if eq <= 0:
            return

        underweight = []
        for ticker in current:
            if ticker not in self.target_weights:
                continue
            target_w  = self.target_weights[ticker]
            price     = self._get_price(ticker, date, use_open=True)
            current_w = price * self.holdings[ticker] / eq if price > 0 else 0
            shortfall = target_w - current_w
            if shortfall >= config.REBALANCE_THRESHOLD:
                underweight.append({"ticker": ticker, "price": price,
                                    "shortfall": shortfall, "target_w": target_w})

        if not underweight:
            return

        total_sf    = sum(s["shortfall"] for s in underweight)
        avail_cash  = self.cash * 0.99
        min_buy     = eq * config.MIN_BUY_AMOUNT_PCT

        for s in underweight:
            if s["price"] <= 0:
                continue
            alloc = avail_cash * (s["shortfall"] / total_sf)
            qty   = int(alloc / (s["price"] * (1 + config.COMMISSION)))
            if qty > 0 and s["price"] * qty >= min_buy and \
               self.cash >= s["price"] * qty * (1 + config.COMMISSION):
                self._buy(s["ticker"], date, s["price"], qty,
                          f"低配補足(目標:{s['target_w']*100:.1f}%)",
                          target_weight=s["target_w"])

    # ── ATR 反比例權重 ────────────────────────────────────────────────────────

    def _calc_atr_weights(self, candidates: list[dict]) -> dict:
        valid = [c for c in candidates if c.get("atr_pct", 0) > 0]
        if not valid:
            return {}
        inv_sum = sum(1 / c["atr_pct"] for c in valid)
        return {c["ticker"]: (1 / c["atr_pct"]) / inv_sum for c in valid}

    # ── 熊市 / SSO 邏輯 ───────────────────────────────────────────────────────

    def _is_dip(self, ticker: str) -> bool:
        """回傳 True 若 ticker 是任何 dip-buy 標的（不受停損/跳空出場影響）。"""
        return ticker in self._dip_tickers

    def _check_bear_sso_logic(self, date):
        state = self.market_regime.get_state(date)
        if not state:
            return

        spy_close  = state["SPY_Close"]
        spy_ma200  = state["SPY_MA200"]
        spy_dd     = abs(state["SPY_DD"])
        is_rebal   = (date.weekday() == config.REBALANCE_WEEKDAY)

        if is_rebal:
            # ── 對齊 live signals.py：counter 用「前一個交易日收盤」判斷 ──
            # Live 在 21:00 TW rebal 時美股還沒開盤，Wed close 不存在，只能看
            # 前一天。Backtest 跟進 → 回測結果忠實反映 live 行為。
            #
            # 實測 2018-07 ~ 2026-04（compare_bull_counter.py）：
            #   前一天版本 vs 當天版本：
            #     CAGR      32.44% vs 32.36%  (+0.08 pp)
            #     Sharpe    1.125 vs 1.116    (+0.009)
            #     Max DD   -29.22% vs -33.10% (+3.88 pp, 顯著改善)
            #     Final eq $8.86M vs $8.81M   (+0.5%)
            # 「慢一週」意外過濾掉假突破 whipsaw，對 max DD 最有利。
            idx = self.spy_df.index.get_indexer([date], method="pad")[0]
            if idx > 0:
                prev_date  = self.spy_df.index[idx - 1]
                prev_state = self.market_regime.get_state(prev_date)
                prev_above = (prev_state["SPY_Close"] > prev_state["SPY_MA200"]
                              if prev_state else False)
            else:
                prev_above = False
            self.bull_weeks_counter = (self.bull_weeks_counter + 1
                                        if prev_above else 0)

        bull_confirmed = (self.bull_weeks_counter >= 2)

        if bull_confirmed:
            # 牛市確認：清掉所有 dip 持倉
            if is_rebal and any(t in self.holdings for t in self._dip_tickers):
                self._close_positions(date, "DIP")
                for k in self.dip_state:
                    self.dip_state[k] = False
            return  # early return，不執行熊市邏輯

        # 熊市邏輯：僅在 SPY < MA200 時才清個股
        if spy_close >= spy_ma200:
            return  # SPY 仍在 MA200 上方，只是牛市週數未滿 2 週，不動作

        stocks = [t for t in self.holdings if t != config.DIP_BUY_TICKER]
        if stocks:
            self._close_positions(date, "STOCK")
            self.target_weights.clear()

        if not config.ENABLE_BEAR_DIP_BUY:
            return

        alloc_map = config.DIP_BUY_ALLOCATIONS
        for level in [0.15, 0.20, 0.25]:
            if spy_dd >= level and not self.dip_state[level]:
                alloc      = alloc_map[level]
                dip_ticker = self.dip_ticker_map[level]
                eq    = (self._get_total_equity(date)
                         if self.compounding else self.initial_capital)
                amt   = min(eq * alloc, self.cash)
                price = self._get_price(dip_ticker, date)
                if price > 0 and amt > 0:
                    qty = int(amt / (price * (1 + config.COMMISSION)))
                    if qty > 0:
                        self._buy(dip_ticker, date, price, qty,
                                  f"熊市抄底 -{level*100:.0f}% ({dip_ticker})")
                        self.dip_state[level] = True

    # 停損 / 跳空出場已移除（ADR-0004）：8 年回測證實 20% 停損零貢獻、
    # 跳空 50% 從未觸發；個股出場改由 _unified_rebalance 的 EMA50/排名輪動處理。

    # ── 交易執行 ──────────────────────────────────────────────────────────────

    def _get_price(self, ticker: str, date: pd.Timestamp,
                   use_open: bool = False) -> float:
        if ticker == "SSO":
            df = self.sso_df
        elif ticker == "SPY":
            df = self.spy_df
        else:
            df = self.selector._get_ticker_data(ticker)

        if df is None or df.empty:
            return 0.0

        col = "Open" if (use_open and "Open" in df.columns) else "Close"

        if date in df.index:
            return float(df.loc[date, col])

        # 指定日期不存在時，取最近一個可用交易日（向前回退）
        idx = df.index.get_indexer([date], method="pad")[0]
        if idx >= 0:
            return float(df.iloc[idx][col])
        return 0.0

    def _buy(self, ticker, date, price, qty, reason, target_weight=None):
        cost = price * qty * (1 + config.COMMISSION)
        self.cash -= cost

        old_qty = self.holdings.get(ticker, 0)
        if old_qty == 0:
            self.holdings[ticker]  = qty
            self.avg_costs[ticker] = price
        else:
            total_val = self.avg_costs[ticker] * old_qty + price * qty
            self.holdings[ticker] += qty
            self.avg_costs[ticker] = total_val / self.holdings[ticker]

        self.trades.append({
            "Date": date, "Ticker": ticker, "Action": "BUY",
            "Price": price, "Quantity": qty, "Cost": cost,
            "Reason": reason, "Revenue": None, "PnL": None,
            "Target_Weight": target_weight * 100 if target_weight else None,
        })

    def _sell(self, ticker, date, price, qty, reason,
              target_weight=None, weight_before=None):
        revenue   = price * qty * (1 - config.COMMISSION)
        self.cash += revenue

        cost_basis = self.avg_costs.get(ticker, 0) * qty * (1 + config.COMMISSION)
        pnl        = revenue - cost_basis
        pnl_pct    = pnl / cost_basis * 100 if cost_basis > 0 else 0

        self.holdings[ticker] -= qty
        if self.holdings[ticker] <= 0:
            del self.holdings[ticker]
            del self.avg_costs[ticker]

        self.trades.append({
            "Date": date, "Ticker": ticker, "Action": "SELL",
            "Price": price, "Quantity": qty, "Cost": None,
            "Reason": reason, "Revenue": revenue, "PnL": pnl,
            "Target_Weight": target_weight * 100 if target_weight else None,
        })

    def _update_equity(self, date):
        eq = self._get_total_equity(date)
        self.history.append({"Date": date, "Equity": eq, "Cash": self.cash})

    def _get_total_equity(self, date) -> float:
        val = sum(self._get_price(t, date) * q
                  for t, q in self.holdings.items())
        return self.cash + val

    def _close_positions(self, date, target_type="ALL"):
        for ticker in list(self.holdings):
            is_dip = self._is_dip(ticker)
            should_sell = (target_type == "ALL" or
                           (target_type == "DIP"   and is_dip) or
                           (target_type == "STOCK" and not is_dip and ticker != "SPY"))
            if should_sell:
                use_open = target_type != "DIP"
                price = self._get_price(ticker, date, use_open=use_open)
                if price > 0:
                    self._sell(ticker, date, price, self.holdings[ticker],
                               f"Clear {target_type}")

    def _force_close_all(self, date):
        self._close_positions(date, "ALL")

    # ── 報告輸出 ──────────────────────────────────────────────────────────────

    def _record_snapshot(self, date, signal_date):
        eq = self._get_total_equity(date)
        holdings_snap = {t: self._get_price(t, date) * q / eq * 100
                         for t, q in self.holdings.items() if q > 0 and eq > 0}
        top20 = self.selector.scan_market(signal_date, config.LOOKBACK)
        if config.MAX_ADJ_SLOPE:
            top20 = [x for x in top20 if x.get("adj_slope", 999) < config.MAX_ADJ_SLOPE]
        top20 = top20[:20]
        self.rebalance_snapshots.append({
            "date": date,
            "holdings": holdings_snap,
            "top20": [x["ticker"] for x in top20],
            "top20_scores": {x["ticker"]: x.get("adj_slope", 0) for x in top20},
        })

    def save_results(self, suffix: str = ""):
        """儲存交易記錄與權益曲線至 output/。"""
        out_dir = config.OUTPUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = f"_{suffix}" if suffix else ""

        if self.trades:
            pd.DataFrame(self.trades).to_csv(
                out_dir / f"sp500_trades{tag}.csv", index=False)

        if self.history:
            df = pd.DataFrame(self.history).set_index("Date")
            df.to_csv(out_dir / f"sp500_equity{tag}.csv")
            return df
        return None

    def get_equity_series(self) -> pd.Series:
        """回傳每日權益 Series（用於組合分析）。"""
        if not self.history:
            return pd.Series(dtype=float)
        df = pd.DataFrame(self.history).set_index("Date")
        return df["Equity"]

    def get_current_holdings(self, date: pd.Timestamp = None) -> dict:
        """回傳目前持倉狀態（用於實盤追蹤）。"""
        if date is None:
            date = self.end_date
        eq = self._get_total_equity(date)
        holdings_list = []
        for ticker, qty in self.holdings.items():
            price    = self._get_price(ticker, date)
            avg_cost = self.avg_costs.get(ticker, 0)
            value    = price * qty
            pnl      = value - avg_cost * qty
            holdings_list.append({
                "ticker":        ticker,
                "qty":           qty,
                "avg_cost":      avg_cost,
                "current_price": price,
                "value":         value,
                "pnl":           pnl,
                "pnl_pct":       pnl / (avg_cost * qty) * 100 if avg_cost * qty > 0 else 0,
                "weight":        value / eq * 100 if eq > 0 else 0,
                "target_weight": self.target_weights.get(ticker, 0) * 100,
            })
        holdings_list.sort(key=lambda x: x["weight"], reverse=True)
        return {"date": date, "holdings": holdings_list,
                "cash": self.cash, "total_equity": eq}
