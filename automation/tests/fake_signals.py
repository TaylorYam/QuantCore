"""signals.StrategySource 的測試替身（#232）：固定的訊號，記下每次被問了什麼。

每個訊號可以給 (權重, 訊號日期) 或一個 callable（拿到同樣的參數，回傳 (權重, 日期)），
需要等待、拋例外或依參數回答時用 callable。SP500 另回 state（預設把 sp500_state
當成這次訊號之後的 state；caller 決定寫不寫）。

    source = FakeSource(sr=({"XLK": 1.0}, "2026-09-28"), universe={"AAPL"})
    plan_inputs.start(ctx, mode, persist_state=False, source=source)
"""
from __future__ import annotations

from signals import StrategySignal


class FakeSource:
    def __init__(self, *, sr=({}, "N/A"), sp500=({}, "N/A"), skewls=({}, "N/A"),
                 universe=(), regime=None, sp500_state=None):
        self._sr, self._sp500, self._skewls = sr, sp500, skewls
        self._universe = universe
        self._regime = regime
        self.sp500_state = {} if sp500_state is None else sp500_state
        self.calls: list[tuple] = []

    @staticmethod
    def _answer(value, *args):
        return value(*args) if callable(value) else value

    def sr(self) -> StrategySignal:
        self.calls.append(("sr",))
        weights, date = self._answer(self._sr)
        return StrategySignal(dict(weights), date)

    def sp500(self, holdings, account_name) -> StrategySignal:
        self.calls.append(("sp500", list(holdings), account_name))
        weights, date = self._answer(self._sp500, holdings, account_name)
        return StrategySignal(dict(weights), date, dict(self.sp500_state))

    def skewls(self) -> StrategySignal:
        self.calls.append(("skewls",))
        weights, date = self._answer(self._skewls)
        return StrategySignal(dict(weights), date)

    def sp500_universe(self, historical: bool = True) -> set[str]:
        self.calls.append(("universe", historical))
        return set(self._answer(self._universe, historical))

    def market_regime(self, signal_date=None) -> dict:
        self.calls.append(("regime", signal_date))
        return dict(self._answer(self._regime, signal_date) or {})

    def called(self, name: str) -> list[tuple]:
        """某一種訊號被問的紀錄（不含名稱），例如 called("sp500") → [(holdings, name)]。"""
        return [c[1:] for c in self.calls if c[0] == name]
