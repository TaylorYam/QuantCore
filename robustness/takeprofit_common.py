"""
QuantCore Robustness — 停利研究共用語意(Phase 1 診斷與 Phase 2 arms 同源)
========================================================================
兩個 load-bearing 定義集中在此,診斷與 arms 回測必須 import 同一份,
不得各自實作(比照 rotation「一顆腦」原則):

1. EpisodeTrackingMixin — 回合(episode)追蹤
   「本回合首筆進場價」語意,含 2026-07-10 對抗審查修正:
   同一週三「賣出又買回」= 續抱(ADR-0005,live 端 combine net 為零、
   不產生訂單),回合**不重設**。歷史 baseline 有 26 次此類 churn,
   全集中在 MRNA/FSLR/NVDA/WDC 等噴出贏家 — 不修會系統性低估
   pnl_pct 並污染門檻錨定。

2. blowoff_factors — 噴出因子(乖離 / rolling skew / rolling kurt)
   全部以「與 _EMA{EXIT_EMA} 同源的調整後收盤序列」計算。
   2026-07-10 對抗審查 blocker 修正:raw Close ÷ Adj-Close EMA 會產生
   時間衰減的股息 artifact(STX 2018 實測 +34.5pp),且調整因子取決於
   快照日之後的股息 = lookahead。live 端資料最後一列 Adj==Close,
   兩種寫法收斂,故統一用調整後序列無 live/backtest 語意分歧。

設計登記:robustness/takeprofit_study_design.md
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.sp500_momentum import config as sp_cfg

SKEW_WINDOWS = [20, 60]
FACTOR_NAMES = ["bias"] + [f"{k}{w}" for k in ("skew", "kurt") for w in SKEW_WINDOWS]

# 研究資料凍結日:夜間排程每日 04:30 追加新 bar(2026-07-11 實證,A0 指標
# 因此漂移),不凍結則診斷/arms/WF 跨日不可重現。門檻錨定於此資料尾。
STUDY_END = "2026-07-09"


class EpisodeTrackingMixin:
    """掛在 SP500Backtester(或其 subclass)前面的回合追蹤 mixin。

    純觀測、零交易行為改動:_buy/_sell 皆為 super() 包裝。
    `episode_entry[ticker] = {"date", "price"}` — 本回合首筆進場。
    """

    def __init__(self, *args, **kwargs):
        self.episode_entry: dict[str, dict] = {}
        self._churn_stash: dict[str, dict] = {}   # 同日賣出暫存,供續抱還原
        super().__init__(*args, **kwargs)

    def _buy(self, ticker, date, price, qty, reason, target_weight=None):
        if self.holdings.get(ticker, 0) == 0 and not self._is_dip(ticker):
            stash = self._churn_stash.pop(ticker, None)
            if stash is not None and stash["date"] == date:
                # 同輪賣出又買回 = 續抱(ADR-0005):回合延續,不重設進場價
                self.episode_entry[ticker] = stash["entry"]
            else:
                self.episode_entry[ticker] = {"date": date, "price": price}
        super()._buy(ticker, date, price, qty, reason, target_weight=target_weight)

    def _sell(self, ticker, date, price, qty, reason,
              target_weight=None, weight_before=None):
        super()._sell(ticker, date, price, qty, reason,
                      target_weight=target_weight, weight_before=weight_before)
        if ticker not in self.holdings:
            entry = self.episode_entry.pop(ticker, None)
            if entry is not None:
                self._churn_stash[ticker] = {"date": date, "entry": entry}


def blowoff_factors(hist: pd.DataFrame,
                    windows: list[int] = SKEW_WINDOWS) -> dict | None:
    """噴出因子。`hist` = 已切到 signal_date(含)為止的個股 DataFrame。

    乖離 = adj_close / EMA{EXIT_EMA}(adj_close) − 1   (單位一致)
    skew/kurt = 調整後日報酬 rolling 高階動差,窗口不足回 NaN。
    資料不足或 EMA 無效回 None。
    """
    if hist is None or len(hist) < max(windows) + 1:
        return None

    col = "Adj Close" if "Adj Close" in hist.columns else "Close"
    ema_col = f"_EMA{sp_cfg.EXIT_EMA}"

    px = float(hist[col].iloc[-1])
    if ema_col in hist.columns:
        ema = float(hist[ema_col].iloc[-1])
    else:
        ema = float(hist[col].ewm(span=sp_cfg.EXIT_EMA, adjust=False).mean().iloc[-1])
    if px <= 0 or not np.isfinite(ema) or ema <= 0:
        return None

    out = {"bias": px / ema - 1.0}
    rets = hist[col].pct_change().dropna()
    for w in windows:
        tail = rets.tail(w)
        out[f"skew{w}"] = float(tail.skew()) if len(tail) == w else np.nan
        out[f"kurt{w}"] = float(tail.kurt()) if len(tail) == w else np.nan
    return out
