# ADR index

架構決策紀錄。**編號只在 main 上分配** — 分支上寫的 ADR 在合回 main 時若撞號，
以 main 為準重編（2026-06 曾發生：分支 0003-0005 與 main 0003-0005 各自長出不同
決策，合併時分支側重編為 0006-0008）。

| # | 決策 | 狀態 |
|---|---|---|
| [0001](0001-alloc-skew-factor-exposure.md) | Skew LS allocation 用 factor-exposure 語意 | accepted |
| [0002](0002-sp500-constituents-on-demand-refresh.md) | SP500 constituents on-demand refresh + dense reconstruction | accepted |
| [0003](0003-live-daily-risk-timing-and-marking.md) | Live 日度風控時間模型與標價來源（Reference Close） | accepted |
| [0004](0004-remove-sp500-fixed-stop-and-gap-exit.md) | 移除 SP500 固定 20% 停損與 50% 跳空出場 | accepted |
| [0005](0005-rank-priority-no-entry-ema-filter.md) | SP500 進場不套 EMA 過濾 — rank 優先（WF 否決） | accepted |
| [0006](0006-sector-level-netting-rejected.md) | Sector-level netting — rejected（leverage drift alpha > friction 省） | rejected |
| [0007](0007-min-position-filter-rejected.md) | SkewLS min-position filter — rejected（小部位是 diversification） | rejected |
| [0008](0008-live-combine-no-weight-level-extraction-rejected.md) | Live combine 不抽 weight-level pure function — rejected | rejected |
| [0009](0009-constituents-gated-auto-refresh.md) | Constituents 每週 gated 自動 refresh（supersede 0002 的 on-demand-only） | accepted |
