"""
QuantCore — Rebalance 決策層（帳戶層的一顆腦）

「快照（NAV + 持倉 + 報價）＋ 各策略權重 → 訂單清單」的唯一實作。
scheduled rebalance（rebalance_ib.main，週間 21:00 Task Scheduler）與
panel Sync Pos（sync_position.compute_sync_plan）皆為 caller —
分類 / mode 權重選擇 / [[Combine]] / 差額門檻 規則變更只改這裡一處。

**純 plain-data 決策，無 I/O**：
- 不連 IB、不讀價格檔、不算訊號、不碰 state 檔 — 快照與權重由 caller
  提供（IB adapter = ib_fetcher；測試 / 離線試算 = fixture dict）。
- execution 關切留在 caller：LMT 價計算（rebalance ±0.05% vs Sync Pos
  bid/ask ±0.3%）、下單順序（先 SELL 後 BUY）、成交等待。
- state 檔讀寫留在 caller：`select_skewls_weights` 只吃已載入的 state
  dict 做純決策，觸發後的 state 更新由 caller 在下單完成後執行。

分類語意 = [[Universe]]（CONTEXT.md）：SP500 用歷史聯集（已移除成分股
仍歸 SP500，2026-01 CIEN 教訓）；SkewLS inverse ETF 是策略持倉不得歸
「其他」（2026-06 凍結事故，fix f327d66）；其餘 = 手動持倉，永不調整。

參數一律呼叫時讀 config（threshold、SR/inverse 清單），兩個 caller
不可能各拿一套。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace as _replace

import numpy as np

log = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────

@dataclass
class PlannedOrder:
    """單一預計執行的調倉委託（決策輸出；未附 LMT 價 — execution 歸 caller）。

    qty 永遠為 abs(delta) > 0（送到 IB 的數量）；action 已記錄方向。
    target / current 可為**負數**（XLC short 等）— 預覽 UI 應顯示符號。
    """
    symbol: str
    action: str          # 'BUY' | 'SELL'  (IB 從 current 推斷 SHORT/COVER)
    qty: int             # > 0 (絕對值)
    price: float         # 決策時的參考價（快照報價）
    strategy: str        # 'SR' | 'SP500' | 'SkewLS' | 'SR+SkewLS' | 'SP500 (hold)' | 'EXIT' | ...
    target: int          # 理論目標股數 (可為負，XLC short)
    current: int         # 當前實際股數 (可為負，已 short)


@dataclass
class HoldingsSplit:
    """IB 持倉的策略歸屬分類（[[Universe]] 語意）。"""
    sr: list[str] = field(default_factory=list)      # SR sector ETF
    sp500: list[str] = field(default_factory=list)   # SP500 個股（歷史聯集）
    other: list[str] = field(default_factory=list)   # 手動持倉 — 永不調整


@dataclass
class DeltaRow:
    """理論 vs 實際的單一標的對帳列（[[Rebalance Plan]] 的觀測輸出）。

    category:
      aligned            — target == current
      within_threshold   — 差額 < REBALANCE_THRESHOLD，設計內不動作
      actionable         — 應執行（超門檻／清倉／建倉／翻向）
      submitted          — 應執行且本輪已送單、等成交（verify_execution 標記）
      manual             — 手動持倉，不參與策略
      excluded_conflict  — 被歸手動卻有策略目標 = 分類異常（凍結類 bug 指紋）
      no_price           — 無報價，本輪跳過
      paused             — 手動持倉進入策略 Universe，策略單暫停（#106，
                           mark_paused 標記；不是凍結類警報）
    """
    symbol: str
    category: str
    reason: str            # 人話原因（TG / panel 顯示用）
    target: int | None     # 理論目標股數；純手動持倉 = None
    current: int
    delta: int             # target − current（manual = 0）
    price: float
    drift_pct: float       # abs(delta) × price ÷ NAV
    strategy: str
    threshold: float       # 判定當下的 REBALANCE_THRESHOLD（顯示用）


@dataclass
class SkewlsDecision:
    """SkewLS threshold gate 的決策結果。"""
    wgt: dict[str, float]        # 進 combine 的 ticker-level signed weights
    signal_date: str
    triggered: bool | None       # None = 本 mode 不檢查；T/F = 檢查結果
    delta: float | None          # should_rebal 的 L1 距離（未檢查 = None）
    threshold: float             # 檢查用門檻（log 顯示用）


# ── 分類 ──────────────────────────────────────────────────────────────

def classify_holdings(positions: dict[str, float],
                      sp500_universe: set[str]) -> HoldingsSplit:
    """
    分辨當前持股：SR ETFs vs SP500 個股 vs 其他（手動操作，排除在外）。

    SkewLS inverse ETF 是策略持倉，不得歸入「其他」— 否則持有後即凍結
    （不加碼/不減碼/不出場；2026-06 三次 threshold 觸發皆單腿執行）。
    sp500_universe 應為**歷史聯集**（[[Universe]]）：已移除成分股仍歸
    SP500，避免 CIEN 類誤分類。

    列表順序 = positions 的迭代順序（log 顯示沿用）。
    """
    from ib_fetcher import SR_ETFS, SKEWLS_INVERSE_TICKERS
    return HoldingsSplit(
        sr=[s for s in positions if s in SR_ETFS],
        sp500=[s for s in positions
               if s not in SR_ETFS and s in sp500_universe],
        other=[s for s in positions
               if s not in SR_ETFS
               and s not in SKEWLS_INVERSE_TICKERS
               and s not in sp500_universe],
    )


# ── Mode 權重選擇 ─────────────────────────────────────────────────────

def sp500_placeholder_weights(current_sp500_holdings: list[str]) -> dict[str, float]:
    """非 SP500 mode 的佔位權重：只為把持股帶進報價清單，不進 combine。"""
    return {sym: 0 for sym in current_sp500_holdings}


def select_skewls_weights(run_skewls: bool, alloc_skewls: float,
                          state: dict,
                          today_target: dict[str, float] | None = None,
                          today_signal_date: str = "N/A") -> SkewlsDecision:
    """
    SkewLS threshold gate（純決策；state 由 caller 載入、觸發後由 caller 更新）。

    - alloc_skewls <= 0     → 空權重（策略未啟用）
    - run_skewls（skewls/all mode）→ should_rebal(state, today_target)：
        觸發 → today_target；未觸發 → state last_target（維持上次持倉）
    - 其他 mode → 一律 state last_target，**不檢查**（triggered=None）—
      確保 SR/SP500 rebal 不會誤砍 SkewLS 倉位（pure-function combine）。
    """
    from strategies.sector_skew_ls import config as skewls_cfg
    import skewls_state
    threshold = skewls_cfg.REBAL_THRESHOLD

    if alloc_skewls <= 0:
        return SkewlsDecision({}, "N/A", None, None, threshold)

    if run_skewls:
        triggered, delta = skewls_state.should_rebal(
            state, today_target, threshold,
        )
        if triggered:
            return SkewlsDecision(dict(today_target), today_signal_date,
                                  True, delta, threshold)
        wgt = dict(state.get("last_target", {}))
        signal_date = state.get("last_target_date") or today_signal_date
        return SkewlsDecision(wgt, signal_date, False, delta, threshold)

    wgt = dict(state.get("last_target", {}))
    signal_date = state.get("last_target_date") or "N/A"
    return SkewlsDecision(wgt, signal_date, None, None, threshold)


# ── Combine（[[Combine]]，ADR-0008：per-strategy 取整 load-bearing）────

def combine_targets(nav: float, sr_wgt: dict, sp500_wgt: dict,
                    prices: dict, ctx,
                    skewls_wgt: dict | None = None) -> dict:
    """
    合併三個策略的目標權重，計算每個標的的目標股數。

    SR 持倉：    target_shares = NAV × alloc_sr     × weight / price
    SP500 持倉： target_shares = NAV × alloc_sp500  × weight / price
    Skew LS：    target_shares = NAV × alloc_skewls × signed_weight / price
                  (XLC 的 weight < 0 → target_shares < 0 → IB SELL → 自動 SHORT)

    三策略共用同一檔 ticker 時 target_shares **直接加總**
    (pure-function combine，CONTEXT.md 已釘住此約定)，IB 自動 net 多空。

    每檔另帶 'strategy_shares'：各 Strategy 加總前的整數股數（依 SR → SP500
    → SkewLS 順序；沒報價而略過的 Strategy 不出現）。逐 Strategy 顯示股數的
    caller 讀這份，不要自己重算取整（#184）。
    """
    alloc_sr    = ctx.alloc_sr
    alloc_sp500 = ctx.alloc_sp500
    alloc_skewls = ctx.alloc_skewls

    # symbol -> {'shares': int, 'strategy': str, 'alloc_w': float,
    #            'strategy_shares': {strategy_tag: int}}
    targets = {}

    def _valid_price(p) -> bool:
        return p is not None and np.isfinite(p) and p > 0

    def _add(sym: str, shares: int, strategy_tag: str, alloc_w: float) -> None:
        """add shares + 合併 strategy tag (使用 '+' 分隔)"""
        if sym in targets:
            targets[sym]['shares']   += shares
            targets[sym]['alloc_w']  += alloc_w
            targets[sym]['strategy_shares'][strategy_tag] = shares
            existing = targets[sym]['strategy'].split('+')
            if strategy_tag not in existing:
                targets[sym]['strategy'] = '+'.join(existing + [strategy_tag])
        else:
            targets[sym] = {'shares': shares, 'strategy': strategy_tag,
                            'alloc_w': alloc_w,
                            'strategy_shares': {strategy_tag: shares}}

    # SR 部位（ETF, long-only）
    for sym, w in sr_wgt.items():
        price = prices.get(sym, 0)
        if not _valid_price(price):
            log.warning(f"  {sym}: no price, skipping (SR)")
            continue
        shares = int(nav * alloc_sr * w / price)
        _add(sym, shares, 'SR', alloc_sr * w)

    # SP500 部位（個股, long-only）
    for sym, w in sp500_wgt.items():
        price = prices.get(sym, 0)
        if not _valid_price(price):
            log.warning(f"  {sym}: no price, skipping (SP500)")
            continue
        # int(round(...)) 而非 floor:高價股 × 小 sleeve 時 floor 系統性低配
        # (Taylor 實測部署率 82~89%),round 誤差對稱 → 長期抵消
        # 三臂回測驗證見 robustness/frac_sizing_report.md (2026-07-05)
        shares = int(round(nav * alloc_sp500 * w / price))
        _add(sym, shares, 'SP500', alloc_sp500 * w)

    # Skew LS 部位（sector ETF / inverse ETF long + XLC short）
    if skewls_wgt:
        for sym, w in skewls_wgt.items():
            price = prices.get(sym, 0)
            if not _valid_price(price):
                log.warning(f"  {sym}: no price, skipping (SkewLS)")
                continue
            # signed: w 可為負 (XLC) → shares 為負 → IB 自動 SHORT
            # int(round(...)) 避免 negative truncation 不對稱
            shares = int(round(nav * alloc_skewls * w / price))
            _add(sym, shares, 'SkewLS', alloc_skewls * w)

    return targets


def apply_sp500_hold(targets: dict, run_sp500: bool,
                     current_sp500_holdings: list[str],
                     positions: dict[str, float]) -> dict:
    """
    非 SP500 mode：把 SP500 持股 target 改回 current（hold）。

    SP500 universe 與 SR/SkewLS 完全分離（個股 vs ETF），override 安全。
    罕見 overlap（SP500 dip-buy 的 SPY/SSO 同時被 SR 或 SkewLS 用）時
    保留 combined target（SR/SkewLS 貢獻），只附註 tag 不覆蓋股數。

    週二 --mode sr 用舊 SP500 訊號誤動持倉（2026-06 CIEN whipsaw，
    ADR-0004）即由此 hold 防護 — SP500 訊號只在 sp500 mode 進 combine。
    """
    if run_sp500:
        return targets
    for sym in current_sp500_holdings:
        if sym not in targets:
            targets[sym] = {'shares': int(positions[sym]),
                            'strategy': 'SP500 (hold)', 'alloc_w': 0}
        else:
            targets[sym]['strategy'] = targets[sym]['strategy'] + '|SP500-hold'
    return targets


# ── 報價清單 + 差額訂單 ───────────────────────────────────────────────

def plan_symbols(sr_wgt: dict, sp500_wgt: dict, skewls_wgt: dict,
                 positions: dict[str, float]) -> list[str]:
    """需要報價的 symbol 全集 = 三策略權重 ∪ 現有持倉（含手動，計 P&L 用）。"""
    return sorted(
        set(sr_wgt) | set(sp500_wgt) | set(skewls_wgt) | set(positions)
    )


def evaluate_deltas(targets: dict[str, dict],
                    current_pos: dict[str, float],
                    current_other_holdings: list[str],
                    prices: dict[str, float],
                    nav: float) -> list[DeltaRow]:
    """
    理論 vs 實際的全帳戶對帳（symbol 排序，決定性輸出）。

    build_order_list 的訂單 = 本表 category == "actionable" 的列 — 分類器與
    下單器同一顆腦，永不分家。判定規則（與歷史行為逐位相同）：
      - 差額 < REBALANCE_THRESHOLD（同向）→ within_threshold，設計內不動作
      - 清倉（target=0）/ 建倉（current=0）/ 翻向（long↔short）→ 必執行
      - 手動持倉（current_other_holdings）→ manual，永不調整；
        若同時**存在策略目標** → excluded_conflict（分類異常 — 2026-06
        inverse 凍結事故的指紋：持倉被誤歸手動、目標單整批蒸發）
    """
    from strategies.sp500_momentum import config as sp_cfg
    rebal_thresh = sp_cfg.REBALANCE_THRESHOLD  # 0.03

    other = set(current_other_holdings)
    strategy_syms = (set(targets.keys()) | set(current_pos.keys())) - other

    rows: list[DeltaRow] = []
    for sym in sorted(strategy_syms):
        price = prices.get(sym, 0)
        target_shares = targets.get(sym, {}).get('shares', 0)
        curr_shares   = int(current_pos.get(sym, 0))
        delta         = target_shares - curr_shares
        strategy      = targets.get(sym, {}).get('strategy', 'EXIT')

        if price <= 0:
            rows.append(DeltaRow(sym, "no_price", "無報價，本輪跳過",
                                 target_shares, curr_shares, delta,
                                 price, 0.0, strategy, rebal_thresh))
            continue
        if delta == 0:
            rows.append(DeltaRow(sym, "aligned", "對齊",
                                 target_shares, curr_shares, 0,
                                 price, 0.0, strategy, rebal_thresh))
            continue

        drift_pct = abs(delta) * price / nav if nav > 0 else 0
        same_direction = (
            target_shares != 0 and curr_shares != 0
            and (target_shares > 0) == (curr_shares > 0)
        )
        if same_direction and drift_pct < rebal_thresh:
            rows.append(DeltaRow(
                sym, "within_threshold",
                f"漂移 {drift_pct*100:.1f}% < {rebal_thresh*100:.0f}% 門檻，設計內不動作",
                target_shares, curr_shares, delta,
                price, drift_pct, strategy, rebal_thresh))
            continue

        if not same_direction:
            if target_shares == 0:
                reason = "清倉（不受門檻限制）"
            elif curr_shares == 0:
                reason = "建倉（不受門檻限制）"
            else:
                reason = "翻向（不受門檻限制）"
        else:
            reason = f"漂移 {drift_pct*100:.1f}% ≥ {rebal_thresh*100:.0f}% 門檻"
        rows.append(DeltaRow(sym, "actionable", reason,
                             target_shares, curr_shares, delta,
                             price, drift_pct, strategy, rebal_thresh))

    # 手動持倉列（報表用；build_order_list 一律略過）
    for sym in sorted(other & (set(current_pos.keys()) | set(targets.keys()))):
        curr = int(current_pos.get(sym, 0))
        price = prices.get(sym, 0)
        if sym in targets:
            t = targets[sym].get('shares', 0)
            rows.append(DeltaRow(
                sym, "excluded_conflict",
                "被歸手動持倉卻有策略目標 — 分類異常，請檢查 universe/清單",
                t, curr, t - curr, price,
                (abs(t - curr) * price / nav) if (nav > 0 and price > 0) else 0.0,
                targets[sym].get('strategy', '?'), rebal_thresh))
        else:
            rows.append(DeltaRow(sym, "manual", "手動持倉，不參與策略調整",
                                 None, curr, 0, price, 0.0, "MANUAL",
                                 rebal_thresh))
    return rows


def build_order_list(targets: dict[str, dict],
                     current_pos: dict[str, float],
                     current_other_holdings: list[str],
                     prices: dict[str, float],
                     nav: float,
                     log_fn=None,
                     warn_fn=None,
                     ignore_threshold: bool = False) -> list[PlannedOrder]:
    """
    根據目標持倉 vs 現有持倉算出差額 order list = evaluate_deltas 的
    actionable 列（規則見該函式；判定同一顆腦）。

    ignore_threshold=True（Sync Pos 全同步語意）：門檻內差額也下單 —
    「Sync 就該拉齊」；使用者在預覽視窗逐單確認後才送。scheduled
    rebalance 一律走預設 False（3% 門檻是設計內的換手抑制）。

    log_fn / warn_fn：scheduled rebalance 傳 log.info / log.warning 以保留
    歷史 log 逐字格式；Sync Pos 傳 None（預覽視窗自行顯示）。
    """
    orders: list[PlannedOrder] = []
    for row in evaluate_deltas(targets, current_pos, current_other_holdings,
                               prices, nav):
        if row.category in ("manual", "excluded_conflict"):
            continue  # 手動持倉不下單（conflict 由對帳報表警示）
        if row.category == "no_price":
            if warn_fn:
                warn_fn(f"  {row.symbol}: no price, skipping")
            continue
        if row.category == "within_threshold" and not ignore_threshold:
            if log_fn:
                log_fn(
                    f"  {row.symbol} [{row.strategy}]: target={row.target:5d}sh "
                    f"current={row.current:5d}sh  delta={row.delta:+d}  "
                    f"drift={row.drift_pct*100:.1f}% < {row.threshold*100:.0f}% → skip"
                )
            continue

        if log_fn:
            log_fn(
                f"  {row.symbol} [{row.strategy}]: target={row.target:5d}sh "
                f"current={row.current:5d}sh  delta={row.delta:+d}"
            )

        if row.category == "actionable" or (
                row.category == "within_threshold" and ignore_threshold):
            orders.append(PlannedOrder(
                symbol=row.symbol,
                action='BUY' if row.delta > 0 else 'SELL',
                qty=abs(row.delta),
                price=row.price,
                strategy=row.strategy,
                target=row.target,
                current=row.current,
            ))
    return orders


def verify_execution(rows: list[DeltaRow],
                     placed_orders: list[dict]) -> list[DeltaRow]:
    """
    執行後驗證：把 actionable 列對照本輪實際送出的單（rebalance_ib 的
    placed dicts）標記覆蓋狀態。MOO 單開盤才成交，故不比成交、只比送單：

      - 有單且數量剛好 → submitted（🕐 等成交，正常）
      - 完全沒有對應單 → 維持 actionable，reason 加註 ⚠️ — 凍結類 bug
        的當場警鈴（決策層算出該動、執行層卻一張單都沒送）
      - 部分覆蓋 → 維持 actionable，reason 註記缺口
    """
    net: dict[str, int] = {}
    for o in placed_orders:
        signed = int(o['qty']) if o['action'] == 'BUY' else -int(o['qty'])
        net[o['symbol']] = net.get(o['symbol'], 0) + signed

    out: list[DeltaRow] = []
    for r in rows:
        if r.category != "actionable":
            out.append(r)
            continue
        covered = net.get(r.symbol, 0)
        residual = r.delta - covered
        if covered == 0:
            out.append(_replace(
                r, reason=r.reason + "；⚠️ 本輪未送出任何對應單"))
        elif residual == 0:
            out.append(_replace(
                r, category="submitted",
                reason="已送單待成交（MOO 開盤成交）"))
        else:
            out.append(_replace(
                r, reason=f"已送 {covered:+d} 股、仍缺 {residual:+d} 股（部分覆蓋）"))
    return out


def mark_paused(rows: list[DeltaRow],
                paused: dict[str, str]) -> list[DeltaRow]:
    """手動持倉暫停（#106）的標的改標 paused，reason = 暫停原因。

    暫停是刻意不送單；照原分類 verify_execution 會標成「該動卻沒送單」🔴，
    每輪都發 Telegram。暫停原因只在開始時通知一次（manual_attribution）。
    paused 為空時原樣回傳。
    """
    if not paused:
        return rows
    return [_replace(r, category="paused", reason=paused[r.symbol])
            if r.symbol in paused and r.category != "manual" else r
            for r in rows]


# 同策略合計偏離的升級門檻 = per-ticker REBALANCE_THRESHOLD 的 2 倍。
# 抓「單檔皆 < 3% 但同策略多檔同向累積」的隱形偏離（凍結遺留的形狀）—
# 這種偏離 scheduled rebalance 永遠不會自動修（每檔都過不了門檻）。
DRIFT_AGG_WARN = 0.06


def strategy_drift_totals(rows: list[DeltaRow]) -> dict[str, float]:
    """各策略 tag 的合計偏離（%NAV）。

    計 within_threshold / actionable / excluded_conflict；
    submitted 排除（已送單、開盤即修，非 standing 偏離）。
    tag 依 row.strategy 原字串分組（'SR+SkewLS' 自成一組）。
    """
    totals: dict[str, float] = {}
    for r in rows:
        if r.category in ("within_threshold", "actionable",
                          "excluded_conflict"):
            totals[r.strategy] = totals.get(r.strategy, 0.0) + r.drift_pct
    return totals


def _red_lines(red: list[DeltaRow]) -> list[str]:
    """🔴 區塊：該動卻沒送（足）單 / 手動分類衝突，逐檔列原因。"""
    lines = [f"🔴 需要注意 {len(red)} 檔:"]
    for r in red:
        tgt = "—" if r.target is None else str(r.target)
        lines.append(f"  {r.symbol} [{r.strategy}]: 應 {tgt} 實 {r.current}"
                     f"（差 {r.delta:+d} 股, {r.drift_pct*100:.1f}% NAV）"
                     f"— {r.reason}")
    return lines


def _aggregate_warn_lines(rows: list[DeltaRow],
                          totals: dict[str, float]) -> list[str]:
    """🟠 同策略累積偏離升級（單檔皆綠也要現形）。"""
    agg_warn = {s: t for s, t in totals.items() if t >= DRIFT_AGG_WARN}
    lines = []
    for strat, tot in sorted(agg_warn.items(), key=lambda x: -x[1]):
        grp = [r for r in rows
               if r.strategy == strat
               and r.category in ("within_threshold", "actionable",
                                  "excluded_conflict")]
        note = ("、單檔皆低於門檻 — 排程不會自動修，請 Sync Pos 或等 TRIGGER"
                if all(r.category == "within_threshold" for r in grp) else "")
        lines.append(f"🟠 {strat} 合計偏離 {tot*100:.1f}% NAV"
                     f"（{len(grp)} 檔累積{note}）")
    return lines


def drift_alert_lines(rows: list[DeltaRow]) -> tuple[list[str], list[str]]:
    """Telegram 調倉通知用的對帳摘要（#26）→ (alarms, advisories)。

    alarms     — 🔴 需要注意、⚠️ 無報價跳過：沒送單也要發（凍結類 bug 的警鈴）
    advisories — 🟠 同策略合計偏離：standing 狀態，只附在已經要發的訊息，
                 不單獨觸發（2026-07-14 起 45 次排程有 15 天出現，會天天洗版）
    🕐 已送單、🟡 門檻內、Σ、⚪ 手動、✅ 對齊 只留在 log（drift_report_lines）。
    """
    red = [r for r in rows if r.category in ("actionable", "excluded_conflict")]
    no_price = [r for r in rows if r.category == "no_price"]
    alarms = _red_lines(red) if red else []
    if no_price:
        alarms.append("⚠️ 無報價跳過: " + ", ".join(r.symbol for r in no_price))
    advisories = _aggregate_warn_lines(rows, strategy_drift_totals(rows))
    return alarms, advisories


def drift_report_lines(rows: list[DeltaRow]) -> list[str]:
    """對帳報表渲染（rebalance log / preview 共用；純文字，一行一項）。"""
    red       = [r for r in rows if r.category in ("actionable",
                                                   "excluded_conflict")]
    submitted = [r for r in rows if r.category == "submitted"]
    yellow    = [r for r in rows if r.category == "within_threshold"]
    manual    = [r for r in rows if r.category == "manual"]
    no_price  = [r for r in rows if r.category == "no_price"]
    paused    = [r for r in rows if r.category == "paused"]
    aligned_n = sum(1 for r in rows if r.category == "aligned")

    lines = ["", "—— 理論 vs 實際 對帳 ——"]
    if red:
        lines.extend(_red_lines(red))

    totals = strategy_drift_totals(rows)
    agg_lines = _aggregate_warn_lines(rows, totals)
    agg_warn = bool(agg_lines)
    lines.extend(agg_lines)

    if paused:
        lines.append(f"⏸ 手動持倉暫停 {len(paused)} 檔: "
                     + ", ".join(f"{r.symbol}{r.delta:+d}" for r in paused))

    if submitted:
        lines.append(f"🕐 已送單待成交 {len(submitted)} 檔: "
                     + ", ".join(f"{r.symbol}{r.delta:+d}" for r in submitted))
    if yellow:
        top = sorted(yellow, key=lambda r: -r.drift_pct)[:3]
        lines.append(f"🟡 門檻內漂移 {len(yellow)} 檔（最大: "
                     + ", ".join(f"{r.symbol} {r.drift_pct*100:.1f}%"
                                 for r in top) + "）")
    if totals:
        lines.append("Σ 偏離 by 策略: " + " | ".join(
            f"{s} {t*100:.1f}%"
            for s, t in sorted(totals.items(), key=lambda x: -x[1])))
    if no_price:
        lines.append("⚠️ 無報價跳過: " + ", ".join(r.symbol for r in no_price))
    if manual:
        lines.append("⚪ 手動排除: " + ", ".join(r.symbol for r in manual))
    lines.append(f"✅ 對齊 {aligned_n} 檔"
                 + ("" if (red or agg_warn or paused) else "，無異常"))
    return lines
