"""
測試 3：加速度計算的邊界情況
─────────────────────────
目的：用合成數據驗證加速度公式在極端情況下的行為。

不需要載入真實資料，純數學驗證。
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np


def calc_acceleration(slope_now, slope_prev, weight=1.0):
    delta = slope_now - slope_prev
    denom = abs(slope_now) if abs(slope_now) > 1e-8 else 1e-8
    raw_ratio = delta / denom
    clipped = np.clip(raw_ratio, -1.0, 1.0)
    adjusted = slope_now * (1 + weight * clipped)
    return {
        "slope_now": slope_now,
        "slope_prev": slope_prev,
        "delta": delta,
        "raw_ratio": raw_ratio,
        "clipped": clipped,
        "adjusted": adjusted,
    }


def test_edge_cases():
    print("=" * 70)
    print("測試 3：邊界情況分析")
    print("=" * 70)

    cases = [
        # (描述, slope_now, slope_prev, weight)
        ("正常加速", 1.5, 1.0, 1.0),
        ("正常減速", 2.0, 2.5, 1.0),
        ("接近零 — 微小正值", 0.001, 0.0005, 1.0),
        ("接近零 — 微小負值", -0.001, -0.002, 1.0),
        ("零交叉：負→正", 0.01, -0.01, 1.0),
        ("零交叉：正→負", -0.01, 0.01, 1.0),
        ("極端加速（翻倍）", 1.0, 0.0, 1.0),
        ("不變", 1.0, 1.0, 1.0),
        ("負動能加速（更負）", -0.5, -0.3, 1.0),
        ("負動能減速（回升）", -0.3, -0.5, 1.0),
        ("高動能微幅加速", 1.5, 1.48, 1.0),
        ("過熱保護邊界 (1.49)", 1.49, 1.0, 1.0),

        # 不同權重
        ("weight=0.2 正常加速", 1.5, 1.0, 0.2),
        ("weight=0.5 正常加速", 1.5, 1.0, 0.5),
        ("weight=0.2 正常減速", 2.0, 2.5, 0.2),
    ]

    print(f"\n{'描述':<25} {'now':>6} {'prev':>6} {'w':>4} "
          f"{'delta':>7} {'ratio':>7} {'clip':>7} {'調整後':>7} {'變化%':>7}")
    print("─" * 95)

    issues = []
    for desc, now, prev, w in cases:
        r = calc_acceleration(now, prev, w)
        pct_change = ((r["adjusted"] - now) / abs(now) * 100
                      if abs(now) > 1e-8 else float("inf"))

        flag = ""
        # 檢查問題
        if abs(r["raw_ratio"]) > 10:
            flag = " [!] ratio爆炸"
            issues.append(f"{desc}: raw_ratio = {r['raw_ratio']:.1f}")
        if abs(pct_change) > 50 and w == 1.0:
            flag += " [!] 變化>50%"
            issues.append(f"{desc}: 調整幅度 {pct_change:+.0f}%")
        if now > 0 and r["adjusted"] < 0:
            flag += " [!] 正變負"
            issues.append(f"{desc}: 正動能被修正為負")

        print(f"{desc:<25} {now:>6.3f} {prev:>6.3f} {w:>4.1f} "
              f"{r['delta']:>+7.3f} {r['raw_ratio']:>+7.2f} {r['clipped']:>+7.2f} "
              f"{r['adjusted']:>7.3f} {pct_change:>+7.1f}%{flag}")

    # ── 排名翻轉測試 ────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("排名翻轉測試")
    print("=" * 70)
    print("情境：5 支股票，加速度修正前後排名是否合理翻轉？\n")

    stocks = [
        # (ticker, slope_now, slope_prev)
        ("FAST_ACCEL", 0.8, 0.4),    # 中等動能，強加速
        ("HIGH_DECEL", 1.5, 2.0),    # 高動能，減速中
        ("STEADY",     1.2, 1.2),    # 穩定高動能
        ("LOW_ACCEL",  0.5, 0.3),    # 低動能，輕微加速
        ("NEG_RECOV",  0.1, -0.2),   # 剛轉正，強回升
    ]

    for w in [0.2, 0.5, 1.0]:
        print(f"\n  weight = {w}")
        print(f"  {'Ticker':<12} {'原始':>6} {'原排名':>6} {'修正後':>7} {'新排名':>6}")
        print(f"  {'─' * 45}")

        orig_ranked = sorted(stocks, key=lambda x: x[1], reverse=True)
        adjusted_list = []
        for ticker, now, prev in stocks:
            r = calc_acceleration(now, prev, w)
            adjusted_list.append((ticker, now, r["adjusted"]))

        adj_ranked = sorted(adjusted_list, key=lambda x: x[2], reverse=True)

        orig_rank = {t: i+1 for i, (t, _, _) in enumerate(
            sorted(stocks, key=lambda x: x[1], reverse=True))}
        adj_rank = {t: i+1 for i, (t, _, _) in enumerate(adj_ranked)}

        for ticker, now, adj in adj_ranked:
            print(f"  {ticker:<12} {now:>6.3f} {orig_rank[ticker]:>6} "
                  f"{adj:>7.3f} {adj_rank[ticker]:>6}")

        # 檢查是否有非直覺的排名
        flips = []
        for t1, _, a1 in adjusted_list:
            for t2, n2, a2 in adjusted_list:
                o1 = next(n for tk, n, _ in stocks if tk == t1)
                o2 = n2
                if o1 > o2 and a1 < a2:
                    flips.append(f"    {t1}(原{o1:.2f}→{a1:.2f}) 被 "
                                 f"{t2}(原{o2:.2f}→{a2:.2f}) 超越")
        if flips:
            print(f"\n  排名翻轉:")
            for f in flips[:5]:
                print(f)

    # ── 問題總結 ─────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("問題總結")
    print("=" * 70)
    if issues:
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
    else:
        print("  未發現邊界問題")


if __name__ == "__main__":
    test_edge_cases()
