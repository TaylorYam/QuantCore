"""Portfolio 快照（#208）：唯讀查詢只讀所選帳戶、分組問決策層，以及和舊
`ib_fetcher.fetch_portfolio` 逐欄一致的 characterization，全部用假 IB。

fetch_snapshot 經 Broker 開連線（#185）：假 IB 接在 broker 的 ib_insync 工廠上，
測的是 adapter 自己的讀法（Connection.portfolio_readings：只讀所選帳戶，和排程同一套）。"""
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import portfolio_snapshot
from account_ctx import AccountCtx
from account_utils import ROLE_PANEL


SCENARIOS = ("empty", "mixed", "foreign", "timeout")


def configure_fake(monkeypatch, module, ctx, scenario):
    class FakeIB:
        disconnected = False
        RequestTimeout = 0

        def connect(self, *args, **kwargs):
            if scenario == "timeout":
                raise TimeoutError("test timeout")

        def managedAccounts(self):
            return [ctx.account]

        def accountValues(self, *args):
            return [NS(account=ctx.account, tag=tag, currency="USD", value=value)
                    for tag, value in [("NetLiquidation", "10000"),
                                       ("Cushion", ".4"), ("ExcessLiquidity", "4000")]]

        def positions(self, account=""):
            if scenario == "empty":
                return []
            held = [NS(account="OTHER" if scenario == "foreign" else ctx.account,
                       contract=NS(symbol=s, secType="STK", longName=""),
                       position=qty, avgCost=100)
                    for s, qty in [("XLK", 2), ("AAPL", 3), ("ZZZ", -1)]]
            # 同 ib_insync：指定帳戶時只回那個帳戶的持倉
            return [p for p in held if not account or p.account == account]

        def isConnected(self):
            return True

        def disconnect(self):
            self.disconnected = True

    fake = FakeIB()

    import broker
    import signals
    monkeypatch.setattr(signals, "get_sp500_universe", lambda: {"AAPL"})
    monkeypatch.setattr(broker, "_default_ib", lambda: fake)
    monkeypatch.setattr(broker.Connection, "prices",
                        lambda self, symbols: {"XLK": 110, "AAPL": 120, "ZZZ": 90})
    return fake


@pytest.mark.parametrize("name", ["Taylor", "Rosa", "Yvonne"])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_legacy_snapshot_characterization(fake_config, monkeypatch, name, scenario):
    """舊 fetch_portfolio 的輸出（fixture）＝新快照的同名欄位，逐欄、逐筆持倉相同。"""
    ctx = AccountCtx.from_name(name, ROLE_PANEL)
    configure_fake(monkeypatch, portfolio_snapshot, ctx, scenario)
    snap = portfolio_snapshot.fetch_snapshot(ctx)
    fixture = Path(__file__).with_name("fixtures") / "portfolio_legacy.json"
    expected = json.loads(fixture.read_text(encoding="utf-8"))[f"{name}/{scenario}"]
    assert legacy_view(snap) == expected


def legacy_view(snap):
    """快照轉回舊 dict 的形狀：只留舊有的 key；error 舊碼只在失敗時才有、debug 只在成功時。"""
    got = asdict(snap)
    for key in ("manual_symbols", "manual_error", "strategy_alloc"):
        del got[key]
    if not got["error"]:
        del got["error"]
    if got["debug"] is None:
        del got["debug"]
    for key in ("etf_holdings", "stock_holdings", "other_holdings"):
        got[key] = list(got[key])
    got["date"] = "FIXED_DATE"
    return got


def test_holdings_are_grouped_by_the_decision_layer(fake_config, monkeypatch):
    """分組問 rebalance_plan.classify_holdings：類股 ETF→ETF、Universe→個股、其他→手動。"""
    import rebalance_plan

    ctx = AccountCtx.from_name("Taylor", ROLE_PANEL)
    configure_fake(monkeypatch, portfolio_snapshot, ctx, "mixed")
    seen = []
    real = rebalance_plan.classify_holdings
    monkeypatch.setattr(rebalance_plan, "classify_holdings",
                        lambda positions, universe: seen.append((dict(positions), universe))
                        or real(positions, universe))
    snap = portfolio_snapshot.fetch_snapshot(ctx)
    assert seen == [({"XLK": 2, "AAPL": 3, "ZZZ": -1}, {"AAPL"})]
    assert [h.symbol for h in snap.etf_holdings] == ["XLK"]
    assert [h.symbol for h in snap.stock_holdings] == ["AAPL"]
    assert [h.symbol for h in snap.other_holdings] == ["ZZZ"]
    assert snap.strategy_mktval == 2 * 110 + 3 * 120 and snap.other_mktval == -90


def test_inverse_etfs_are_strategy_etfs_not_manual(fake_config, monkeypatch):
    """SkewLS 反向 ETF 歸 ETF 組（策略持倉），不是手動（2026-06 凍結事故）。"""
    import broker

    ctx = AccountCtx.from_name("Taylor", ROLE_PANEL)
    fake = configure_fake(monkeypatch, portfolio_snapshot, ctx, "empty")
    fake.positions = lambda account="": [NS(account=ctx.account,
                                            contract=NS(symbol="ERY", secType="STK", longName=""),
                                            position=5, avgCost=20)]
    monkeypatch.setattr(broker.Connection, "prices", lambda self, symbols: {"ERY": 21})
    snap = portfolio_snapshot.fetch_snapshot(ctx)
    assert [h.symbol for h in snap.etf_holdings] == ["ERY"] and snap.other_holdings == ()
    assert snap.strategy_mktval == 105


def test_readonly_rejects_wrong_account_and_cleans_up(fake_config, monkeypatch):
    ctx = AccountCtx.from_name("Taylor", ROLE_PANEL)
    fake = configure_fake(monkeypatch, portfolio_snapshot, ctx, "empty")
    fake.managedAccounts = lambda: ["OTHER"]
    snap = portfolio_snapshot.fetch_snapshot(ctx, readonly=True)
    assert not snap.available
    assert "Account" in snap.error
    assert fake.disconnected


def test_readonly_does_not_borrow_foreign_positions_or_nav(fake_config, monkeypatch):
    ctx = AccountCtx.from_name("Taylor", ROLE_PANEL)
    fake = configure_fake(monkeypatch, portfolio_snapshot, ctx, "foreign")
    snap = portfolio_snapshot.fetch_snapshot(ctx, readonly=True)
    assert snap.available and snap.nav == 10000
    assert not (snap.etf_holdings or snap.stock_holdings or snap.other_holdings)
    fake.accountValues = lambda *args: [NS(account="OTHER", tag="NetLiquidation", currency="USD", value="999")]
    snap = portfolio_snapshot.fetch_snapshot(ctx, readonly=True)
    assert not snap.available
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

    import broker
    monkeypatch.setattr(broker, "_default_ib", FakeIB)
    with pytest.raises(TimeoutError):
        broker.Broker().connect(ctx, timeout=20, readonly=True)
    assert calls[0]["readonly"] is True and calls[0]["account"] == ctx.account
    assert calls[-1] == "disconnect"


class _FeedIB:
    """報價 feed port 上的假 IB：記下連線參數，每檔都有價。"""

    def __init__(self, calls):
        self.calls = calls

    def connect(self, *a, **kw):
        self.calls.append(kw)
        assert kw.get("readonly") is not True or self.RequestTimeout == 20

    def qualifyContracts(self, *a):
        pass

    def reqTickers(self, *contracts):
        return [NS(contract=c, midpoint=lambda: 123) for c in contracts]

    def isConnected(self):
        return True

    def disconnect(self):
        self.calls.append("disconnect")


def _no_live_price_ib(port):
    """本連線沒有即時報價（feed 借到就不會走到延遲報價）。"""
    missing = lambda c: NS(contract=c, midpoint=lambda: float("nan"), last=0, close=0)
    return NS(client=NS(port=port), qualifyContracts=lambda *a: None,
              reqTickers=lambda *cs: [missing(c) for c in cs])


def test_readonly_price_feed_ids_are_independent(fake_config):
    import broker
    calls = []
    feed_broker = broker.Broker(lambda: _FeedIB(calls))
    for name in ("Taylor", "Rosa", "Yvonne"):
        ctx = AccountCtx.from_name(name, ROLE_PANEL)
        # 本連線的 port 設成 0：Rosa 自己就在 feed port 上，這裡要它也去借價
        conn = broker.Connection(_no_live_price_ib(0), ctx, readonly=True,
                                 broker=feed_broker)
        assert conn.prices(["XLK"]) == {"XLK": 123}
    connects = [c for c in calls if c != "disconnect"]
    assert len({c["clientId"] for c in connects}) == 3
    assert all(c["readonly"] for c in connects)
    # 一般連線借報價用固定的 99（規則在 broker.price_feed_client_id）
    ctx = AccountCtx.from_name("Taylor", ROLE_PANEL)
    broker.Connection(_no_live_price_ib(0), ctx, broker=feed_broker).prices(["XLK"])
    assert calls[-2]["clientId"] == 99 and "readonly" not in calls[-2]


def test_feed_connection_receives_unique_id_and_closes():
    import broker
    calls = []
    ctx = NS(name="Taylor", account="U1", host="127.0.0.1", port=4001, client_id=25,
             price_feed_port=4002)
    conn = broker.Connection(_no_live_price_ib(4001), ctx, readonly=True,
                             broker=broker.Broker(lambda: _FeedIB(calls)))
    assert conn.prices(["XLK"]) == {"XLK": 123}
    assert calls[0]["clientId"] == 1025 and calls[0]["readonly"] is True
    assert calls[0]["timeout"] == 10 and calls[0]["account"] == ""
    assert calls[-1] == "disconnect"
