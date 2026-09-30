"""新 Panel 查權益／持倉（#149）。

查詢本身沿用 `ib_fetcher.fetch_portfolio`（和 Tk Panel 同一套，唯讀連線、查完就斷），
只換 clientId：新 Panel 的 Gateway 心跳已經佔著 ROLE_PANEL_READONLY 那組號碼，
查詢改用 `get_qt_panel_query_client_id`（Taylor 69／Rosa 79／Yvonne 89），不和
心跳、Tk Panel、排程搶同一個 clientId（ADR-0012）。
"""
from __future__ import annotations

from dataclasses import replace


def fetch_readonly_portfolio(account_name: str) -> dict:
    import ib_fetcher
    from account_ctx import AccountCtx
    from account_utils import ROLE_PANEL, get_qt_panel_query_client_id

    ctx = replace(AccountCtx.from_name(account_name, ROLE_PANEL),
                  client_id=get_qt_panel_query_client_id(account_name))
    return ib_fetcher.fetch_portfolio(ctx, readonly=True)
