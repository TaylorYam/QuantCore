"""新 Panel 查權益／持倉（#149），以及單一帳戶頁的權益序列、策略設定、最近成交（#150）。

查詢本身沿用 `ib_fetcher.fetch_portfolio`（和 Tk Panel 同一套，唯讀連線、查完就斷），
只換 clientId：新 Panel 的 Gateway 心跳已經佔著 ROLE_PANEL_READONLY 那組號碼，
查詢改用 `get_qt_panel_query_client_id`（Taylor 69／Rosa 79／Yvonne 89），不和
心跳、Tk Panel、排程搶同一個 clientId（ADR-0012）。

另外唯讀讀一次手動歸屬紀錄的暫停標的（碰撞進策略 Universe 的手動持倉），持倉表
才能把它們標成手動（CONTEXT「手動持倉」）；讀不到就在結果裡寫原因，不影響讀數。
"""
from __future__ import annotations

from dataclasses import replace


def fetch_readonly_portfolio(account_name: str) -> dict:
    import ib_fetcher
    from account_ctx import AccountCtx
    from account_utils import ROLE_PANEL, get_qt_panel_query_client_id

    ctx = replace(AccountCtx.from_name(account_name, ROLE_PANEL),
                  client_id=get_qt_panel_query_client_id(account_name))
    result = ib_fetcher.fetch_portfolio(ctx, readonly=True)
    if result.get("available"):
        result.update(read_manual_symbols(account_name))
    return result


def read_manual_symbols(account_name: str, path=None) -> dict:
    """{"manual_symbols": 標的 → 暫停原因}；紀錄讀取失敗時改回 {"manual_error": 原因}。"""
    from manual_attribution import ManualAttributionError, paused_symbols

    try:
        return {"manual_symbols": paused_symbols(account_name, path)}
    except ManualAttributionError as e:
        return {"manual_error": str(e)}


# ── 單一帳戶頁的其他資料（#150）：都只讀檔，不連 IB ─────────────────

def load_equity(account_name: str):
    """每日 NAV（和 Telegram 權益曲線同一個來源）。"""
    from equity_chart import load_equity_series
    return load_equity_series(account_name)


def read_strategy_allocations(account_name: str) -> dict:
    """帳戶的三個 Allocation（唯讀；只取 alloc 欄位，不把 token 等設定帶進畫面）。"""
    from account_utils import get_account_config
    from strategy_params_display import STRATEGIES
    cfg = get_account_config(account_name)
    return {key: cfg[key] for key, *_ in STRATEGIES if key in cfg}


def load_account_cash_flows(account_name: str):
    """出入金（ib_flex.FlowsResult）：IB Flex 報表＋本機快取，同一天只下載一次。

    token／Query ID 在 accounts.json 該帳戶的 flex_token／flex_query_id（唯讀）。
    """
    from datetime import datetime

    from account_utils import load_accounts
    from ib_flex import CashFlowCache, load_cash_flows

    cfg = next((a for a in load_accounts() if a["name"] == account_name), {})
    return load_cash_flows(account_name, cfg, CashFlowCache(), now=datetime.now())


def read_recent_trades(account_name: str) -> list:
    from trades_display import read_latest_trades
    return read_latest_trades(account_name)
