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
1. 不連 IB — 本模組不 import ib_fetcher 的連線函式（classify 所需的
   SR_ETFS / SKEWLS_INVERSE_TICKERS 常數由 rebalance_plan 內部取用）。
2. 不寫 state — `block_state_writes()` 於整段決策期間攔截
   sp500_state.save_state（get_sp500_weights 每次呼叫必存 — 含 dip-buy /
   bull counter 推進，攔截是必要而非防禦）與 skewls_state.save_state
   （rebalance_ib step 10 的觸發後更新在預演中不存在，純防禦）。
   sp500 / skewls state 一律唯讀 load，決策與 live 用同一份 state。
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
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterator, Optional

# ── 路徑設定（與 rebalance_ib 相同慣例）─────────────────────────────────
BASE_DIR = Path(__file__).parent
ROOT_DIR = BASE_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pandas as pd

from account_ctx import AccountCtx
from account_utils import ROLE_REBALANCE
import rebalance_plan as plan
import signals
import skewls_state
import sp500_state

log = logging.getLogger(__name__)

DATA_DIR = ROOT_DIR / "data"

MODES = ("sr", "sp500", "skewls", "both", "all")
MODE_LABEL = {
    "sr": "SR Only", "sp500": "SP500 Only",
    "skewls": "SkewLS Only", "both": "SR+SP500", "all": "All 3",
}


# ── 零寫入 guard ──────────────────────────────────────────────────────

@contextmanager
def block_state_writes() -> Iterator[list[str]]:
    """
    攔截 sp500 / skewls 的 save_state — 預演期間任何 state 寫入都改為記錄。

    yield 出的 list 收集被攔下的呼叫（"模組:帳戶"），供報表揭露。
    離開（含例外）時還原原函式，pytest 內重複使用不留殘影。
    """
    blocked: list[str] = []
    orig_sp500 = sp500_state.save_state
    orig_skewls = skewls_state.save_state

    def _block_sp500(account_name: str, state: dict) -> None:
        blocked.append(f"sp500_state:{account_name}")
        log.info(f"  [預演] 攔截 sp500_state.save_state({account_name!r}) — 不落地")

    def _block_skewls(account_name: str, state: dict) -> None:
        blocked.append(f"skewls_state:{account_name}")
        log.info(f"  [預演] 攔截 skewls_state.save_state({account_name!r}) — 不落地")

    sp500_state.save_state = _block_sp500
    skewls_state.save_state = _block_skewls
    try:
        yield blocked
    finally:
        sp500_state.save_state = orig_sp500
        skewls_state.save_state = orig_skewls


# ── 報價（本地 CSV 最新收盤；replay_hotfix 同款）─────────────────────────

def local_close(symbol: str, data_dir: Path | None = None) -> Optional[float]:
    """data/etfs、data/stocks 最新收盤；兩處皆無檔 → None。"""
    base = Path(data_dir) if data_dir else DATA_DIR
    for sub in ("etfs", "stocks"):
        p = base / sub / f"{symbol}.csv"
        if p.exists():
            try:
                return float(pd.read_csv(p)["Close"].iloc[-1])
            except Exception as e:
                log.warning(f"  {symbol}: 讀取 {p.name} 失敗 ({e})")
                return None
    return None


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

    run_sp500 = mode in ("sp500", "both", "all")
    run_skewls = mode in ("skewls", "all")

    with block_state_writes() as blocked:
        # 1. SR 訊號（所有 mode 都算 — combine overlap 需要，與 live 相同）
        sr_wgt, sr_date = signals.get_sr_weights()

        # 3. 分類持倉（[[Universe]] 歷史聯集語意）
        universe = signals.get_sp500_universe()
        split = plan.classify_holdings(positions, universe)
        log.info(f"  SR holdings:    {split.sr or '(none)'}")
        log.info(f"  SP500 holdings: {split.sp500 or '(none)'}")
        if split.other:
            log.info(f"  OTHER holdings: {split.other} (excluded from strategy)")

        # 4. SP500 訊號 — 只在 sp500/both/all 跑（ADR-0004 hold 防護）；
        #    save_state 已被 block_state_writes 攔截
        if run_sp500:
            sp500_wgt, sp500_date = signals.get_sp500_weights(split.sp500,
                                                              ctx.name)
        else:
            sp500_wgt = plan.sp500_placeholder_weights(split.sp500)
            sp500_date = "N/A"

        # 4b. SkewLS：唯讀 load state → threshold gate 純決策
        sk: plan.SkewlsDecision | None = None
        skewls_wgt: dict[str, float] = {}
        if ctx.alloc_skewls > 0:
            state = skewls_state.load_state(ctx.name)   # 唯讀
            today_target: dict[str, float] = {}
            today_date = "N/A"
            if run_skewls:
                today_target, today_date = signals.get_skewls_weights()
            sk = plan.select_skewls_weights(
                run_skewls, ctx.alloc_skewls, state,
                today_target=today_target,
                today_signal_date=today_date,
            )
            skewls_wgt = sk.wgt
            if run_skewls:
                log.info(f"  SkewLS threshold: Δ={sk.delta:.4f} vs {sk.threshold}"
                         f" → {'TRIGGER' if sk.triggered else 'hold'}")

        # 5. 報價（snapshot 優先，缺的 fallback 本地收盤）
        all_syms = plan.plan_symbols(sr_wgt, sp500_wgt, skewls_wgt, positions)
        prices, missing = resolve_prices(all_syms, snapshot_prices, price_lookup)

        # 6. Combine + hold + 差額訂單（決策層唯一實作，ADR-0008 內部不動）
        sp500_wgt_for_combine = sp500_wgt if run_sp500 else {}
        targets = plan.combine_targets(nav, sr_wgt, sp500_wgt_for_combine,
                                       prices, ctx=ctx, skewls_wgt=skewls_wgt)
        targets = plan.apply_sp500_hold(targets, run_sp500, split.sp500,
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
        sr_wgt=sr_wgt, sr_date=sr_date,
        sp500_wgt=sp500_wgt, sp500_date=sp500_date,
        skewls=sk,
        prices=prices, targets=targets,
        current_pos=dict(positions), split=split,
        missing_prices=missing,
        blocked_state_writes=list(blocked),
    )


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
    pos: dict[str, float] = {}
    for tok in re.split(r"[,\s]+", spec.strip()):
        if not tok:
            continue
        if "=" not in tok:
            raise ValueError(f"持倉格式應為 SYM=QTY，收到 {tok!r}")
        sym, _, qty = tok.partition("=")
        sym = sym.strip().upper()
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
    nav = float(raw["nav"])
    positions = {str(s).upper(): float(q)
                 for s, q in dict(raw["positions"]).items()}
    prices = {str(s).upper(): float(p)
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
