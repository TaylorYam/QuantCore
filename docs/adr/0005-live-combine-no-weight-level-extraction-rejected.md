# Live combine 不抽 weight-level pure function — per-strategy 整數 rounding 是 load-bearing

考慮過把 `automation/rebalance_ib.py::combine_targets` 的 per-ticker netting 不變量抽成一個純函式 `combine_weights({strategy: weights}, allocs) -> {ticker: combined_weight}`，動機是「讓 [[Combine]] domain 不變量成為獨立 test surface、並與 backtest 共用同一實作」（架構 review 候選 2）。Reject —— 三個理由：

1. **`combine_targets` 現況已是 reasonably-deep 且測透的模組。** interface = `(nav, 各策略權重 dict, prices, allocs)`，只用 `ctx` 的三個 alloc 欄位，用 fake ctx 即可測、不需穿過 IB。`automation/tests/test_combine_targets.py` 有 16 個測試，其中 `test_sr_sp500_overlap_merged`、`test_skewls_overlap_with_sr_sums_correctly` **直接測重疊 ticker 的加總**（= ADR-0003 釘住的 netting 約定）。不變量「已經是」test surface，候選 2 的主要動機不成立。

2. **weight-level 合併會改變 live 行為。** 現況是**每個策略各自** `int(nav × alloc × w / price)` 算出股數、再把同 ticker 的股數相加。若改成「先合併權重、再換算一次」`int(nav × Σ(alloc×w) / price)`，對重疊 ticker 而言 `int(a) + int(b) ≠ int(a+b)`（例：`int(2.6)+int(2.6)=4` 但 `int(5.2)=5`）。[[SR]] 與 [[Skew LS]] 都交易 sector ETF，重疊很常見 → 重疊 ticker 的 live 目標股數會漂移。而且 SR/SP500 用 `int()` 截斷、Skew LS 用 `int(round())`（避免 negative truncation 不對稱，見 [[Direct Short]]）—— **per-strategy rounding 是刻意設計**，合併權重會把這個區分抹掉。

3. **live 與 backtest 的 combine 本來就在不同 level，無法用單一純函式統一。** backtest 的 `backtest/combo.py::combine_monthly` 在 **return level** 運作（月初重設目標比例 + 月內自由 drift + 融資成本扣減），live 的 `combine_targets` 在 **ticker-level shares**。兩者是不同運算，硬要共用會逼其中一邊改形狀。

deletion test：刪掉 `combine_targets`，複雜度不會「重新出現在多個 caller」（它已集中且測過）→ 它已經 earning its keep，不是 shallow pass-through。

代價：live 與 backtest 的 combine 概念上各自實作（但本來就在不同 level，非架構問題）。真正值得收斂的 combine 重複是 **return-level** 那條 —— `shared/run_backtest.py::combine_50_50` 重抄了 `backtest/combo.py` 已 export 的同名 alias（架構 review 候選 5，便宜且與 live 無關）。

若未來改用 fractional shares（IB 支援）使整數 rounding 不再 load-bearing，可重評本決策。
