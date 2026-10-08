"""Panel 查權益／持倉（#149），以及單一帳戶頁的權益序列、策略設定、最近成交（#150）。

查詢用 `portfolio_snapshot.fetch_snapshot`（經 Broker 開唯讀連線、查完就斷，#208），
身分是 ROLE_PANEL：不和 Gateway 心跳（ROLE_MONITOR）、排程搶同一個 clientId。

另外唯讀讀一次手動歸屬紀錄的暫停標的（碰撞進策略 Universe 的手動持倉），持倉表
才能把它們標成手動（CONTEXT「手動持倉」）；讀不到就在結果裡寫原因，不影響讀數。
"""
from __future__ import annotations

from dataclasses import replace


def fetch_panel_portfolio(account_name: str):
    """這個帳戶的 PortfolioSnapshot，帶上持倉歸屬要的資料。"""
    import portfolio_snapshot
    from account_ctx import AccountCtx
    from account_utils import ROLE_PANEL

    ctx = AccountCtx.from_name(account_name, ROLE_PANEL)
    return _with_attribution(portfolio_snapshot.fetch_snapshot(ctx, readonly=True), ctx)


def _with_attribution(snapshot, ctx):
    """持倉歸屬要的額外資料：碰撞的手動持倉，和 Allocation（決定做多類股 ETF 歸 SR
    還是 SkewLS，#181）。建立新的快照，不改原本那份。"""
    if not snapshot.available:
        return snapshot
    return replace(snapshot, **read_manual_symbols(ctx.name),
                   strategy_alloc=(ctx.alloc_sr, ctx.alloc_skewls))


def read_manual_symbols(account_name: str, path=None) -> dict:
    """{"manual_symbols": 標的 → 暫停原因}；紀錄讀取失敗時改回 {"manual_error": 原因}。"""
    from manual_attribution import ManualAttributionError, paused_symbols

    try:
        return {"manual_symbols": paused_symbols(account_name, path)}
    except ManualAttributionError as e:
        return {"manual_error": str(e)}


# ── 單一帳戶頁的其他資料（#150）：都只讀檔，不連 IB ─────────────────

def load_equity(account_name: str):
    """每日 NAV（portfolio_tracker.xlsx 的 Daily）。"""
    from ledger import daily_nav
    return daily_nav(account_name)


def read_strategy_allocations(account_name: str) -> dict:
    """帳戶的三個 Allocation（唯讀；只取 alloc 欄位，不把 token 等設定帶進畫面）。"""
    from account_utils import get_account_config
    from strategy_params_display import STRATEGIES
    cfg = get_account_config(account_name)
    return {key: cfg[key] for key, *_ in STRATEGIES if key in cfg}


def write_strategy_allocations(account_name: str, allocations: dict) -> None:
    """把帳戶的 Allocation 寫進 accounts.json 該帳戶的覆寫值（#220）。

    Panel 寫 accounts.json 只走這裡：只收三個 Allocation 欄位、每個 0–100%、
    合計不超過上限，其他欄位（leverage、port、account 等）一律拒絕。
    """
    from account_utils import update_account_config
    from strategy_params_display import MAX_TOTAL_ALLOCATION, STRATEGIES
    allowed = {key for key, *_ in STRATEGIES}
    other = sorted(set(allocations) - allowed)
    if other:
        raise ValueError(f"只能寫 Allocation 欄位，不能寫 {other}")
    for key, value in allocations.items():
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{key} = {value} 超出 0–100%")
    if round(sum(allocations.values()), 2) > MAX_TOTAL_ALLOCATION:
        raise ValueError(f"Allocation 合計超過 {MAX_TOTAL_ALLOCATION:.0%}")
    update_account_config(account_name, dict(allocations))


def load_account_cash_flows(account_name: str):
    """出入金（ib_flex.FlowsResult）：IB Flex 報表＋本機快取，同一天只下載一次。

    token／Query ID 在 accounts.json 該帳戶的 flex_token／flex_query_id（唯讀）。
    """
    from datetime import datetime

    from account_utils import load_accounts
    from ib_flex import CashFlowCache, account_entry, load_cash_flows

    cfg = account_entry(load_accounts(), account_name) or {}
    return load_cash_flows(account_name, cfg, CashFlowCache(), now=datetime.now())


def read_recent_trades(account_name: str) -> list:
    from trades_display import read_account_trades
    return read_account_trades(account_name)
