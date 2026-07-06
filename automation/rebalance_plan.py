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
from dataclasses import dataclass, field

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
    """
    alloc_sr    = ctx.alloc_sr
    alloc_sp500 = ctx.alloc_sp500
    alloc_skewls = ctx.alloc_skewls

    targets = {}  # symbol -> {'shares': int, 'strategy': str, 'alloc_w': float}

    def _valid_price(p) -> bool:
        return p is not None and np.isfinite(p) and p > 0

    def _add(sym: str, shares: int, strategy_tag: str, alloc_w: float) -> None:
        """add shares + 合併 strategy tag (使用 '+' 分隔)"""
        if sym in targets:
            targets[sym]['shares']   += shares
            targets[sym]['alloc_w']  += alloc_w
            existing = targets[sym]['strategy'].split('+')
            if strategy_tag not in existing:
                targets[sym]['strategy'] = '+'.join(existing + [strategy_tag])
        else:
            targets[sym] = {'shares': shares, 'strategy': strategy_tag,
                            'alloc_w': alloc_w}

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


def build_order_list(targets: dict[str, dict],
                     current_pos: dict[str, float],
                     current_other_holdings: list[str],
                     prices: dict[str, float],
                     nav: float,
                     log_fn=None,
                     warn_fn=None) -> list[PlannedOrder]:
    """
    根據目標持倉 vs 現有持倉算出差額 order list（symbol 排序，決定性輸出）。

    套用 REBALANCE_THRESHOLD 過濾：單一標的漂移 < 3% NAV 不動作。
    例外：清倉（target=0）/ 建倉（current=0）/ 翻向（long↔short）必執行。
    手動持倉（current_other_holdings）永不產生訂單。

    log_fn / warn_fn：scheduled rebalance 傳 log.info / log.warning 以保留
    歷史 log 逐字格式；Sync Pos 傳 None（預覽視窗自行顯示）。
    """
    from strategies.sp500_momentum import config as sp_cfg
    rebal_thresh = sp_cfg.REBALANCE_THRESHOLD  # 0.03

    all_target_syms = set(targets.keys()) | set(current_pos.keys())
    all_target_syms -= set(current_other_holdings)  # 排除手動操作標的

    orders: list[PlannedOrder] = []
    for sym in sorted(all_target_syms):
        price = prices.get(sym, 0)
        if price <= 0:
            if warn_fn:
                warn_fn(f"  {sym}: no price, skipping")
            continue

        target_shares = targets.get(sym, {}).get('shares', 0)
        curr_shares   = int(current_pos.get(sym, 0))
        delta         = target_shares - curr_shares
        strategy      = targets.get(sym, {}).get('strategy', 'EXIT')

        # REBALANCE_THRESHOLD 過濾：小於 3% NAV 漂移不動作
        # 例外：清倉（target=0）/ 開倉（current=0）/ 翻向（long↔short）必須執行
        same_direction = (
            target_shares != 0 and curr_shares != 0
            and (target_shares > 0) == (curr_shares > 0)
        )
        if delta != 0 and same_direction:
            drift_val = abs(delta) * price
            drift_pct = drift_val / nav if nav > 0 else 0
            if drift_pct < rebal_thresh:
                if log_fn:
                    log_fn(
                        f"  {sym} [{strategy}]: target={target_shares:5d}sh "
                        f"current={curr_shares:5d}sh  delta={delta:+d}  "
                        f"drift={drift_pct*100:.1f}% < {rebal_thresh*100:.0f}% → skip"
                    )
                continue

        if log_fn:
            log_fn(
                f"  {sym} [{strategy}]: target={target_shares:5d}sh "
                f"current={curr_shares:5d}sh  delta={delta:+d}"
            )

        if delta != 0:
            orders.append(PlannedOrder(
                symbol=sym,
                action='BUY' if delta > 0 else 'SELL',
                qty=abs(delta),
                price=price,
                strategy=strategy,
                target=target_shares,
                current=curr_shares,
            ))
    return orders
