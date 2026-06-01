# Skew LS allocation uses factor-exposure semantics

`alloc_skewls` 是 scalar multiplier 套在 strategy normalized weights 上（與 SR / SP500 一致），語意為「對 sector skewness 因子的曝險佔 NAV 的百分比」。考慮過用 capital-usage 語意（讓策略內部 rescale 使 IB margin 占用 = alloc_skewls），但會破壞與既有策略的對稱性、改變 daily PnL、必須重做 walk-forward；且會放棄 2x 反向 ETF 的資金效率紅利。代價是 UI 顯示的 1.5x leverage 跟 IB margin 占用會差 5-10%（因為 Inverse Proxy 用一半資金達同樣曝險）。
