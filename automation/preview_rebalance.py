#!/usr/bin/env python3
"""
QuantCore — Rebalance 離線預演（不連 IB、零 state 寫入）

用 scheduled rebalance 的同一顆決策腦（rebalance_plan + signals）重放
「快照 → 訂單」全鏈，輸出預期訂單表 + 理論 vs 實際對帳，但**不落地**：

    classify_holdings → 訊號 (SR / SP500 / SkewLS) → select_skewls_weights
      → combine_targets → apply_sp500_hold → build_order_list

輸入（擇一）
------------
(a) --snapshot snap.json
    {"nav": 16401.49,
     "positions": {"XLU": 50, "BTC": 65.1783},
     "prices":    {"XLU": 82.1}}     # prices 可省略／部分提供 —
                                     # 缺的 symbol fallback 本地 CSV 最新收盤
(b) --nav 16401.49 --pos "XLU=50,BTC=65.1783"（或 --pos-file pos.json）
    報價一律取 data/etfs、data/stocks 最新收盤（--data-dir 可改資料根目錄）

訊號永遠由本地 data CSV 重算（與 live 同一套 signals.* 函式）；snapshot
只提供 NAV / 持倉 / 報價，不影響訊號本身。fixtures 形狀沿用 replay_hotfix
（2026-07 inverse hotfix 驗證）的 NAV + positions 慣例。

安全保證（離線預演三不）
------------------------
1. 不連 IB — 本模組不 import broker 的連線（classify 所需的類股／反向 ETF 清單
   由 rebalance_plan 向 strategy_owner 取用）。
2. 不寫 state — 調倉輸入（plan_inputs）以 persist_state=False 取得：SP500
   訊號只讀 state 不寫回（含 dip-buy / bull counter 推進），本來會寫的 state
   記在 skipped_state_writes 供報表揭露；預演不做 rebalance_ib step 10 的
   SkewLS 觸發後更新。sp500 / skewls state 一律唯讀 load，決策與 live 用同一份
   state。
3. 不動 accounts.json / config.json — AccountCtx.from_name 唯讀。

combine_targets 內部不動（ADR-0008）— 本模組只是 rebalance_plan 的第三個
caller（scheduled rebalance、panel Sync Pos 之後）。mode 語意與
rebalance_ib.main 相同（sr / sp500 / skewls / both / all；非 sp500 mode
的 SP500 hold 防護 = ADR-0004）。

用法範例
--------
  python preview_rebalance.py --account Taylor --mode sp500 --snapshot snap.json
  python preview_rebalance.py --account Rosa --mode sr --nav 37974.73 \
      --pos "XLU=99,XLV=104,BTC=65.1783"
  python preview_rebalance.py --account Yvonne --mode all --snapshot snap.json \
      --json preview_out.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

# ── 路徑設定（與 rebalance_ib 相同慣例）─────────────────────────────────
BASE_DIR = Path(__file__).parent
ROOT_DIR = BASE_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from account_ctx import AccountCtx
from account_utils import ROLE_REBALANCE
import plan_inputs
import rebalance_plan as plan
from data.market_data import data_root

log = logging.getLogger(__name__)

DATA_DIR = data_root()

MODES = ("sr", "sp500", "skewls", "both", "all")
MODE_LABEL = {
    "sr": "SR Only", "sp500": "SP500 Only",
    "skewls": "SkewLS Only", "both": "SR+SP500", "all": "All 3",
}



# ── 報價（本地 CSV 最新收盤；replay_hotfix 同款）─────────────────────────

def local_close(symbol: str, data_dir: Path | None = None) -> Optional[float]:
    """data/etfs、data/stocks 最新收盤；兩處皆無檔或檔不能用 → None（data/market_data 寫 log）。"""
    from data import market_data
    from symbol_contract import canonical_symbol

    return market_data.last_close(canonical_symbol(symbol),
                                  root=Path(data_dir) if data_dir else DATA_DIR)


def resolve_prices(symbols: list[str],
                   snapshot_prices: dict[str, float] | None = None,
                   price_lookup: Callable[[str], Optional[float]] | None = None,
                   ) -> tuple[dict[str, float], list[str]]:
    """
    snapshot 報價優先；缺的（或非正數）用 price_lookup（預設本地 CSV 收盤）；
    仍缺 → 0.0 並列入 missing（決策層自會歸 no_price，不下單）。
    """
    lookup = price_lookup or local_close
    snap = snapshot_prices or {}
    prices: dict[str, float] = {}
    missing: list[str] = []

    def _valid(p) -> bool:
        return p is not None and math.isfinite(p) and p > 0

    for sym in symbols:
        px = snap.get(sym)
        if not _valid(px):
            px = lookup(sym)
        if _valid(px):
            prices[sym] = float(px)
        else:
            prices[sym] = 0.0
            missing.append(sym)
    return prices, missing


# ── 預演結果 ──────────────────────────────────────────────────────────

@dataclass
class PreviewResult:
    """離線預演的完整輸出（形狀對齊 sync_position.SyncPlan）。"""
    ctx: AccountCtx
    mode: str
    nav: float
    orders: list[plan.PlannedOrder]         # 預期訂單（symbol 排序）
    rows: list[plan.DeltaRow]               # 理論 vs 實際全帳戶對帳
    report_lines: list[str]                 # drift_report_lines 渲染
    sr_wgt: dict[str, float]
    sr_date: str
    sp500_wgt: dict[str, float]
    sp500_date: str
    skewls: plan.SkewlsDecision | None      # alloc_skewls <= 0 時為 None
    prices: dict[str, float]
    targets: dict[str, dict]                # combine + hold 後的目標
    current_pos: dict[str, float]
    split: plan.HoldingsSplit
    missing_prices: list[str] = field(default_factory=list)
    blocked_state_writes: list[str] = field(default_factory=list)


# ── 核心：離線重放決策鏈 ──────────────────────────────────────────────

def compute_preview(mode: str, ctx: AccountCtx, nav: float,
                    positions: dict[str, float],
                    snapshot_prices: dict[str, float] | None = None,
                    price_lookup: Callable[[str], Optional[float]] | None = None,
                    ) -> PreviewResult:
    """
    以快照（NAV + 持倉 [+ 報價]）重放 rebalance_ib.main 的決策段
    （step 1 / 3 / 4 / 4b / 5 / 6），回傳預期訂單與對帳 — 不連 IB、
    不寫 state、不下單。

    Parameters
    ----------
    mode : "sr" | "sp500" | "skewls" | "both" | "all"（同 rebalance_ib）
    ctx  : AccountCtx（alloc 比例來源；唯讀）
    nav / positions : 快照
    snapshot_prices : 部分報價（缺的 fallback price_lookup）
    price_lookup    : symbol -> price|None（預設 local_close；測試可注入）
    """
    if mode not in MODES:
        raise ValueError(f"mode 必須是 {MODES}，收到 {mode!r}")
    if nav is None or not math.isfinite(nav) or nav <= 0:
        raise ValueError(f"NAV 必須為正數，收到 {nav!r}")

    # 1–5. 調倉輸入（唯一實作 = plan_inputs）：SR 訊號（所有 mode 都算 —
    #   combine overlap 需要）→ 分類持倉（[[Universe]] 歷史聯集語意）→ SP500 只在
    #   sp500/both/all 跑（ADR-0004 hold 防護）→ SkewLS 唯讀 load state + threshold
    #   gate → 報價（snapshot 優先，缺的 fallback 本地收盤）。不寫 state、不更新
    #   手動持倉歸屬。
    account = SnapshotAccount(nav, positions, snapshot_prices, price_lookup)
    inputs = plan_inputs.start(ctx, mode, persist_state=False,
                               narrator=_PreviewSteps())
    inputs.read(account, attribution=False)
    for skipped in inputs.skipped_state_writes:
        module, account_name = skipped.split(":", 1)
        log.info(f"  [預演] 攔截 {module}.save_state({account_name!r}) — 不落地")
    split = inputs.split
    prices = inputs.prices

    # 6. Combine + hold + 差額訂單（決策層唯一實作，ADR-0008 內部不動）
    targets = plan.combine_targets(nav, inputs.sr_wgt, inputs.sp500_for_combine,
                                   prices, ctx=ctx, skewls_wgt=inputs.skewls_wgt)
    targets = plan.apply_sp500_hold(targets, inputs.run_sp500, split.sp500,
                                    positions)

    orders = plan.build_order_list(
        targets=targets,
        current_pos=positions,
        current_other_holdings=split.other,
        prices=prices,
        nav=nav,
        log_fn=log.info,
        warn_fn=log.warning,
    )
    rows = plan.evaluate_deltas(targets, positions, split.other,
                                prices, nav)
    report_lines = plan.drift_report_lines(rows)

    return PreviewResult(
        ctx=ctx, mode=mode, nav=nav,
        orders=orders, rows=rows, report_lines=report_lines,
        sr_wgt=inputs.sr_wgt, sr_date=inputs.sr_date,
        sp500_wgt=inputs.sp500_wgt, sp500_date=inputs.sp500_date,
        skewls=inputs.skewls_decision,
        prices=prices, targets=targets,
        current_pos=dict(positions), split=split,
        missing_prices=account.missing,
        blocked_state_writes=list(inputs.skipped_state_writes),
    )


class SnapshotAccount:
    """預演的帳戶快照（plan_inputs 的 adapter）：給定 NAV／持倉；報價 snapshot
    優先，缺的用 price_lookup（預設本地 CSV 收盤），取不到的記在 missing。"""

    def __init__(self, nav: float, positions: dict[str, float],
                 snapshot_prices: dict[str, float] | None = None,
                 price_lookup: Callable[[str], Optional[float]] | None = None):
        self._nav = nav
        self._positions = positions
        self._snapshot_prices = snapshot_prices
        self._price_lookup = price_lookup
        self.missing: list[str] = []

    def nav(self) -> float:
        return self._nav

    def positions(self) -> dict[str, float]:
        return self._positions

    def prices(self, symbols: list[str]) -> dict[str, float]:
        prices, self.missing = resolve_prices(symbols, self._snapshot_prices,
                                              self._price_lookup)
        return prices


class _PreviewSteps(plan_inputs.Narrator):
    """預演的 log：持倉分類與 SkewLS threshold 結果。"""

    def holdings(self, split: plan.HoldingsSplit) -> None:
        log.info(f"  SR holdings:    {split.sr or '(none)'}")
        log.info(f"  SP500 holdings: {split.sp500 or '(none)'}")
        if split.other:
            log.info(f"  OTHER holdings: {split.other} (excluded from strategy)")

    def skewls_gate(self, sk: plan.SkewlsDecision) -> None:
        log.info(f"  SkewLS threshold: Δ={sk.delta:.4f} vs {sk.threshold}"
                 f" → {'TRIGGER' if sk.triggered else 'hold'}")


# ── 渲染 ──────────────────────────────────────────────────────────────

def render_report(res: PreviewResult) -> list[str]:
    """預演報表（純文字，一行一項；console / 檔案共用）。"""
    ctx = res.ctx
    lines = [
        "=" * 60,
        f"  QuantCore Rebalance 預演  {date.today()}  [{MODE_LABEL[res.mode]}]",
        "  離線模式：未連 IB / 未寫 state / 未下單",
        f"  Account: {ctx.name}   NAV: ${res.nav:,.2f}"
        f"  →  部位 ${res.nav * ctx.leverage:,.0f} ({ctx.leverage}x)",
        "  Alloc: SR {:.0%} + SP500 {:.0%}".format(ctx.alloc_sr, ctx.alloc_sp500)
        + (f" + SkewLS {ctx.alloc_skewls:.0%}" if ctx.alloc_skewls > 0 else "")
        + f" = {ctx.leverage:.0%}",
        "=" * 60,
        f"SR Signal: {res.sr_date}  |  SP500 Signal: {res.sp500_date}",
    ]
    if res.skewls is not None:
        sk = res.skewls
        if sk.triggered is None:
            gate = "未檢查（沿用 last_target）"
        elif sk.triggered:
            gate = f"✅ 觸發 Δ={sk.delta:.4f} > {sk.threshold}"
        else:
            gate = f"⏸ 未觸發 Δ={sk.delta:.4f} ≤ {sk.threshold}（沿用 last_target）"
        lines.append(f"SkewLS Signal: {sk.signal_date}  [threshold {gate}]")

    sells = [o for o in res.orders if o.action == "SELL"]
    buys = [o for o in res.orders if o.action == "BUY"]
    lines.append("")
    lines.append(f"—— 預期訂單 ({len(res.orders)} 筆；先 SELL 後 BUY）——")
    if not res.orders:
        lines.append("  (無 — 已在目標上)")
    for o in sells + buys:
        lines.append(
            f"  {o.action:<4s} {o.qty:>5d}sh {o.symbol:<6s}"
            f" @${o.price:>8.2f}  ~${o.qty * o.price:>10,.0f}"
            f"  [{o.strategy}]  {o.current} → {o.target}"
        )

    if res.missing_prices:
        lines.append("⚠️ 無報價（snapshot 未提供且本地 CSV 缺檔）: "
                     + ", ".join(res.missing_prices))
    if res.blocked_state_writes:
        lines.append(f"🛡 預演攔截 state 寫入 {len(res.blocked_state_writes)} 次: "
                     + ", ".join(res.blocked_state_writes))
    else:
        lines.append("🛡 預演期間無任何 state 寫入嘗試")

    lines.extend(res.report_lines)
    return lines


def result_to_dict(res: PreviewResult) -> dict:
    """canonical dict（orders / rows 皆 symbol 排序）— 供 --json 與 diff 比對。"""
    ctx = res.ctx
    sk = res.skewls
    return {
        "schema": "preview_rebalance/1",
        "generated": str(date.today()),
        "account": ctx.name,
        "mode": res.mode,
        "nav": res.nav,
        "alloc": {"sr": ctx.alloc_sr, "sp500": ctx.alloc_sp500,
                  "skewls": ctx.alloc_skewls, "leverage": ctx.leverage},
        "signals": {
            "sr_date": res.sr_date,
            "sp500_date": res.sp500_date,
            "skewls_date": sk.signal_date if sk else "N/A",
            "skewls_triggered": sk.triggered if sk else None,
            "skewls_delta": sk.delta if sk else None,
        },
        "orders": [asdict(o) for o in
                   sorted(res.orders, key=lambda o: o.symbol)],
        "delta_rows": [asdict(r) for r in
                       sorted(res.rows, key=lambda r: r.symbol)],
        "missing_prices": sorted(res.missing_prices),
        "blocked_state_writes": list(res.blocked_state_writes),
    }


# ── CLI ───────────────────────────────────────────────────────────────

def parse_positions(spec: str) -> dict[str, float]:
    """解析 "XLU=50, BTC=65.1783"（逗號/空白分隔）→ {symbol: qty}。"""
    from symbol_contract import canonical_symbol

    pos: dict[str, float] = {}
    for tok in re.split(r"[,\s]+", spec.strip()):
        if not tok:
            continue
        if "=" not in tok:
            raise ValueError(f"持倉格式應為 SYM=QTY，收到 {tok!r}")
        sym, _, qty = tok.partition("=")
        sym = canonical_symbol(sym)
        if not sym:
            raise ValueError(f"持倉格式應為 SYM=QTY，收到 {tok!r}")
        pos[sym] = float(qty)
    return pos


def load_snapshot(path: str | Path) -> tuple[float, dict[str, float],
                                             dict[str, float]]:
    """讀 snapshot JSON → (nav, positions, prices)；缺必要鍵 → ValueError。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if "nav" not in raw or "positions" not in raw:
        raise ValueError(f"snapshot 必須含 'nav' 與 'positions' 鍵: {path}")
    from symbol_contract import canonical_symbol

    nav = float(raw["nav"])
    positions = {canonical_symbol(str(s)): float(q)
                 for s, q in dict(raw["positions"]).items()}
    prices = {canonical_symbol(str(s)): float(p)
              for s, p in dict(raw.get("prices") or {}).items()}
    return nav, positions, prices


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        stream=sys.stdout)

    parser = argparse.ArgumentParser(
        description="QuantCore Rebalance 離線預演（不連 IB、零 state 寫入）")
    parser.add_argument("--account", required=True,
                        help="accounts.json 的帳戶名（Taylor/Rosa/Yvonne）— 唯讀")
    parser.add_argument("--mode", choices=list(MODES), default="both",
                        help="同 rebalance_ib：sr(週二) / sp500(週三) / "
                             "skewls / both / all（預設 both）")
    parser.add_argument("--snapshot", default=None,
                        help="快照 JSON（nav/positions[/prices]）— 與 "
                             "--nav/--pos/--pos-file 互斥")
    parser.add_argument("--nav", type=float, default=None,
                        help="手動輸入 NAV（非 snapshot 模式必填）")
    parser.add_argument("--pos", default=None,
                        help='手動輸入持倉："XLU=50,BTC=65.1783"')
    parser.add_argument("--pos-file", default=None,
                        help="持倉 JSON 檔 {symbol: qty}（與 --pos 可併用，--pos 優先）")
    parser.add_argument("--data-dir", default=None,
                        help=f"報價 CSV 根目錄（預設 {DATA_DIR}）")
    parser.add_argument("--json", default=None,
                        help="把 canonical 結果另存 JSON（diff 比對用）")
    args = parser.parse_args(argv)

    if args.snapshot:
        if args.nav is not None or args.pos or args.pos_file:
            parser.error("--snapshot 與 --nav/--pos/--pos-file 互斥")
        nav, positions, snap_prices = load_snapshot(args.snapshot)
    else:
        if args.nav is None or not (args.pos or args.pos_file):
            parser.error("非 snapshot 模式需要 --nav 且 --pos / --pos-file 至少一個")
        positions = {}
        if args.pos_file:
            raw = json.loads(Path(args.pos_file).read_text(encoding="utf-8-sig"))
            positions.update({str(s).upper(): float(q)
                              for s, q in dict(raw).items()})
        if args.pos:
            positions.update(parse_positions(args.pos))
        nav, snap_prices = args.nav, {}

    price_lookup = None
    if args.data_dir:
        _dir = Path(args.data_dir)
        price_lookup = lambda s: local_close(s, _dir)  # noqa: E731

    ctx = AccountCtx.from_name(args.account, ROLE_REBALANCE)  # 唯讀
    res = compute_preview(args.mode, ctx, nav, positions,
                          snapshot_prices=snap_prices,
                          price_lookup=price_lookup)

    print("\n".join(render_report(res)))

    if args.json:
        out = Path(args.json)
        out.write_text(json.dumps(result_to_dict(res), indent=1,
                                  sort_keys=True, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\ncanonical JSON → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
