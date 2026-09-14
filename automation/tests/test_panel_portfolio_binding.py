"""Exercise actual Panel methods without importing its admin/startup side effects."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

from portfolio_queries import QueryState


def panel_methods():
    source = Path(__file__).resolve().parents[1] / "control_panel.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ControlPanel")
    wanted = {"_on_portfolio_change", "_show_cached_portfolio", "_refresh_ib_live"}
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    namespace = {}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def test_late_result_cannot_overwrite_selected_account_and_switch_is_cache_only():
    methods = panel_methods()
    displayed, logs = [], []
    selected = ["Yvonne"]
    yvonne = QueryState(portfolio={"available": True, "nav": 123})
    panel = NS(selected_account=NS(get=lambda: selected[0]),
               portfolio_view=NS(update_state=lambda *args: displayed.append(args)),
               portfolio_queries=NS(snapshot=lambda name: yvonne), _log=logs.append)
    methods["_on_portfolio_change"](panel, "Taylor", QueryState(error="timeout"))
    assert not displayed
    methods["_on_portfolio_change"](panel, "Yvonne", yvonne)
    assert displayed == [("Yvonne", yvonne)]
    methods["_show_cached_portfolio"](panel)
    assert displayed[-1] == ("Yvonne", yvonne)
    # No refresh API exists on the fake: selecting cached data cannot query IB.


def test_switching_to_no_data_clears_other_holdings_and_chart():
    from portfolio_view import PortfolioView
    rendered = {}

    def widget(name):
        return NS(config=lambda **kw: rendered.setdefault(name, {}).update(kw))

    view = PortfolioView.__new__(PortfolioView)
    view.t = {"text_dim": "dim", "yellow": "yellow"}
    for key in ("nav_card", "pnl_card", "margin_card", "strat_exp_card", "total_exp_card"):
        setattr(view, key, NS(_val_lbl=widget(key)))
    for key in ("report_date_lbl", "etf_list", "stock_list", "other_list", "etf_hdr", "stock_hdr", "other_hdr"):
        setattr(view, key, widget(key))
    destroyed = []
    view.chart_canvas = NS(get_tk_widget=lambda: NS(destroy=lambda: destroyed.append(True)))
    view.update_state("Rosa", QueryState(error="timeout"))
    assert destroyed == [True] and view.chart_canvas is None
    assert rendered["other_list"]["text"] == "(no data)"
    assert all(rendered[key]["text"] == "" for key in ("etf_hdr", "stock_hdr", "other_hdr"))
    assert "Rosa | Update failed" in rendered["report_date_lbl"]["text"]
    assert "No data yet" in rendered["report_date_lbl"]["text"]


def test_readonly_harness_uses_test_identity_without_credentials(tmp_path):
    import json
    from check_portfolio_queries import load_contexts
    folder = tmp_path / "automation"
    folder.mkdir()
    (folder / "config.json").write_text(json.dumps({"telegram_token": "FAKE_SECRET", "dry_run": False}))
    (folder / "accounts.json").write_text(json.dumps([
        {"name": name, "account": f"FAKE{i}", "port": 4001+i}
        for i, name in enumerate(("Taylor", "Rosa", "Yvonne"))]))
    contexts = load_contexts(tmp_path)
    assert len({ctx.client_id for ctx in contexts.values()}) == 3
    assert all(ctx.client_id > 16000 and ctx.dry_run and not ctx.tg_token
               for ctx in contexts.values())
