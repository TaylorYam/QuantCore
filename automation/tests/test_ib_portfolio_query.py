"""Read-only query isolation and legacy portfolio characterization, all fake IB.

fetch_portfolio 經 Broker 開連線（#185）：假 IB 接在 broker 的 ib_insync 工廠上，
測的是 adapter 自己的讀法（Connection.portfolio_readings 的多層 fallback）。"""
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import ib_fetcher
from account_ctx import AccountCtx
from account_utils import ROLE_PANEL


SCENARIOS = ("empty", "mixed", "foreign", "fallback", "timeout")


def configure_fake(monkeypatch, module, ctx, scenario):
    class FakeIB:
        disconnected = False
        RequestTimeout = 0

        def connect(self, *args, **kwargs):
            if scenario == "timeout":
                raise TimeoutError("test timeout")

        def managedAccounts(self):
            return [ctx.account]

        def accountSummary(self, *args):
            return [] if scenario == "fallback" else self.accountValues()

        def accountValues(self, *args):
            return [NS(account=ctx.account, tag=tag, currency="USD", value=value)
                    for tag, value in [("NetLiquidation", "10000"),
                                       ("Cushion", ".4"), ("ExcessLiquidity", "4000")]]

        def reqAccountUpdates(self, *args):
            pass

        def sleep(self, *args):
            pass

        def positions(self):
            if scenario in ("empty", "fallback"):
                return []
            return [NS(account="OTHER" if scenario == "foreign" else ctx.account,
                       contract=NS(symbol=s, secType="STK", longName=""),
                       position=qty, avgCost=100)
                    for s, qty in [("XLK", 2), ("AAPL", 3), ("ZZZ", -1)]]

        def isConnected(self):
            return True

        def disconnect(self):
            self.disconnected = True

    fake = FakeIB()

    import broker
    import signals
    monkeypatch.setattr(signals, "get_sp500_universe", lambda: {"AAPL"})
    monkeypatch.setattr(broker, "_default_ib", lambda: fake)
    monkeypatch.setattr(module, "get_prices", lambda *a, **kw: {"XLK": 110, "AAPL": 120, "ZZZ": 90})
    return fake


@pytest.mark.parametrize("name", ["Taylor", "Rosa", "Yvonne"])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_legacy_snapshot_characterization(fake_config, monkeypatch, name, scenario):
    ctx = AccountCtx.from_name(name, ROLE_PANEL)
    configure_fake(monkeypatch, ib_fetcher, ctx, scenario)
    result = ib_fetcher.fetch_portfolio(ctx)
    result["date"] = "FIXED_DATE"
    fixture = Path(__file__).with_name("fixtures") / "portfolio_legacy.json"
    expected = json.loads(fixture.read_text(encoding="utf-8"))[f"{name}/{scenario}"]
    assert json.dumps(result, sort_keys=True) == json.dumps(expected, sort_keys=True)


def test_readonly_rejects_wrong_account_and_cleans_up(fake_config, monkeypatch):
    ctx = AccountCtx.from_name("Taylor", ROLE_PANEL)
    fake = configure_fake(monkeypatch, ib_fetcher, ctx, "empty")
    fake.managedAccounts = lambda: ["OTHER"]
    result = ib_fetcher.fetch_portfolio(ctx, readonly=True)
    assert not result["available"]
    assert "Account" in result["error"]
    assert fake.disconnected


def test_readonly_does_not_borrow_foreign_positions_or_nav(fake_config, monkeypatch):
    ctx = AccountCtx.from_name("Taylor", ROLE_PANEL)
    fake = configure_fake(monkeypatch, ib_fetcher, ctx, "foreign")
    result = ib_fetcher.fetch_portfolio(ctx, readonly=True)
    assert result["available"] and result["nav"] == 10000
    assert not any(result[key] for key in ("etf_holdings", "stock_holdings", "other_holdings"))
    fake.accountValues = lambda *args: [NS(account="OTHER", tag="NetLiquidation", currency="USD", value="999")]
    result = ib_fetcher.fetch_portfolio(ctx, readonly=True)
    assert not result["available"]
    assert fake.disconnected


def test_readonly_connect_is_bounded_and_cleanup_on_failure(fake_config, monkeypatch):
    ctx = AccountCtx.from_name("Taylor", ROLE_PANEL)
    calls = []

    class FakeIB:
        def connect(self, *args, **kwargs):
            calls.append(kwargs)
            assert self.RequestTimeout == 20
            raise TimeoutError("fail")

        def disconnect(self):
            calls.append("disconnect")

    monkeypatch.setattr(ib_fetcher, "IB", FakeIB)
    with pytest.raises(TimeoutError):
        ib_fetcher.connect(ctx, readonly=True)
    assert calls[0]["readonly"] is True and calls[0]["account"] == ctx.account
    assert calls[-1] == "disconnect"


def test_readonly_price_feed_ids_are_independent(fake_config, monkeypatch):
    import price_utils
    options = []
    monkeypatch.setattr(price_utils, "robust_get_prices", lambda *a, **kw: options.append(kw))
    for name in ("Taylor", "Rosa", "Yvonne"):
        ib_fetcher.get_prices(None, ["XLK"], AccountCtx.from_name(name, ROLE_PANEL), readonly=True)
    assert len({kw["feed_client_id"] for kw in options}) == 3
    assert all(kw["readonly"] for kw in options)
    ib_fetcher.get_prices(None, ["XLK"], AccountCtx.from_name("Taylor", ROLE_PANEL))
    # 一般連線借報價用固定的 99（同 price_utils 原本的預設），規則在 broker（#185）
    assert options[-1]["readonly"] is False and options[-1]["feed_client_id"] == 99


def test_feed_connection_receives_unique_id_and_closes(monkeypatch):
    import ib_insync
    import price_utils
    calls = []
    contract = NS(symbol="XLK")
    missing = NS(contract=contract, midpoint=lambda: float("nan"), last=0, close=0)
    priced = NS(contract=contract, midpoint=lambda: 123)

    class Feed:
        def connect(self, *a, **kw):
            calls.append(kw)
            assert self.RequestTimeout == 20

        def qualifyContracts(self, *a):
            pass

        def reqTickers(self, *a):
            return [priced]

        def isConnected(self):
            return True

        def disconnect(self):
            calls.append("disconnect")

    monkeypatch.setattr(ib_insync, "IB", Feed)
    own = NS(client=NS(port=4001), qualifyContracts=lambda *a: None,
             reqTickers=lambda *a: [missing])
    assert price_utils.robust_get_prices(own, ["XLK"], feed_client_id=1025, readonly=True) == {"XLK": 123}
    assert calls[0]["clientId"] == 1025 and calls[0]["readonly"] is True
    assert calls[-1] == "disconnect"
