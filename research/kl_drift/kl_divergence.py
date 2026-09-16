"""
kl_divergence.py
KL 散度工具模組 — 用於量化兩個報酬率分佈之間的差異。

主要用途：偵測市場狀態切換（例如：低波動轉高波動、趨勢轉震盪）。
所有計算僅依賴 numpy 與 pandas，不引入繪圖套件。
"""

import numpy as np
import pandas as pd


# 預設分箱邊界（7 個區間）
# 大跌 | 中跌 | 小跌 | 平盤 | 小漲 | 中漲 | 大漲
_DEFAULT_BINS = [-np.inf, -0.03, -0.01, -0.003, 0.003, 0.01, 0.03, np.inf]


def compute_kl(p: np.ndarray, q: np.ndarray, epsilon: float = 1e-8) -> float:
    """
    計算離散 KL 散度 D_KL(P || Q)。

    KL 散度衡量分佈 P 相對於參考分佈 Q 的「資訊損失」。
    值越大代表兩個分佈差異越大；值為 0 代表完全相同。

    注意：KL 散度不具對稱性，D_KL(P||Q) ≠ D_KL(Q||P)。

    參數
    ----
    p : np.ndarray
        當前分佈（機率直方圖），各元素非負且總和應接近 1。
    q : np.ndarray
        參考分佈（機率直方圖），各元素非負且總和應接近 1。
    epsilon : float
        平滑係數，避免 log(0) 或除以零的數值問題。預設 1e-8。

    回傳
    ----
    float
        KL 散度值（單位：nats，使用自然對數）。
    """
    p = np.asarray(p, dtype=float)
    q = np.asarray(q, dtype=float)

    if p.shape != q.shape:
        raise ValueError(f"p 與 q 的形狀必須相同，但得到 {p.shape} 和 {q.shape}")

    # 加上 epsilon 平滑，防止 log(0) 或 0/0
    p_smooth = p + epsilon
    q_smooth = q + epsilon

    # 重新正規化，確保總和為 1
    p_smooth = p_smooth / p_smooth.sum()
    q_smooth = q_smooth / q_smooth.sum()

    # D_KL(P || Q) = sum( P * log(P / Q) )
    kl = np.sum(p_smooth * np.log(p_smooth / q_smooth))

    return float(kl)


def returns_to_histogram(returns: np.ndarray, bins=None) -> np.ndarray:
    """
    將每日報酬率陣列轉換為離散機率分佈（正規化直方圖）。

    預設分箱邊界（7 個區間）：
        (-∞, -3%]           → 大跌
        (-3%, -1%]          → 中跌
        (-1%, -0.3%]        → 小跌
        (-0.3%, +0.3%)      → 平盤
        [+0.3%, +1%)        → 小漲
        [+1%, +3%)          → 中漲
        [+3%, +∞)           → 大漲

    參數
    ----
    returns : np.ndarray
        每日報酬率陣列（例如：0.01 代表漲 1%）。
    bins : list 或 None
        自訂分箱邊界（含頭尾 -inf / +inf）。
        若為 None，使用上述預設 7 個區間。

    回傳
    ----
    np.ndarray
        長度等於區間數的機率陣列，總和為 1。
    """
    returns = np.asarray(returns, dtype=float)

    if bins is None:
        bins = _DEFAULT_BINS

    # np.histogram 計算各區間的計數
    counts, _ = np.histogram(returns, bins=bins)

    total = counts.sum()

    if total == 0:
        # 若資料為空，回傳均勻分佈（避免後續除以零）
        n_bins = len(bins) - 1
        return np.ones(n_bins) / n_bins

    # 正規化為機率
    probabilities = counts / total

    return probabilities.astype(float)


def rolling_kl(
    returns_series: pd.Series,
    ref_window: int = 252,
    current_window: int = 60,
    step: int = 5,
) -> pd.Series:
    """
    計算滾動 KL 散度時間序列。

    對每個計算點 t，定義：
        P_ref     = returns_series[t - ref_window - current_window : t - current_window]
                    （歷史參考窗口，代表「過去的正常狀態」）
        P_current = returns_series[t - current_window : t]
                    （當前窗口，代表「最近的市場狀態」）

    KL 值越高，代表當前市場分佈偏離歷史參考越遠（可能發生狀態切換）。

    參數
    ----
    returns_series : pd.Series
        每日報酬率，必須具備 DatetimeIndex，按時間升序排列。
    ref_window : int
        參考歷史窗口大小（交易日數）。預設 252（約一年）。
    current_window : int
        當前觀察窗口大小（交易日數）。預設 60（約一季）。
    step : int
        計算步長（每隔幾個交易日計算一次）。預設 5（每週）。

    回傳
    ----
    pd.Series
        KL 散度序列，Index 為對應的日期（DatetimeIndex）。
        若資料不足以建立任何窗口，回傳空 Series。
    """
    returns_series = returns_series.dropna()

    # 最少需要 ref_window + current_window 筆資料
    min_required = ref_window + current_window
    n = len(returns_series)

    if n < min_required:
        return pd.Series(dtype=float, name="kl_divergence")

    values = returns_series.values  # 轉為 numpy 陣列加速計算
    dates = returns_series.index

    kl_dates = []
    kl_values = []

    # 從第一個可用點開始，每隔 step 個交易日計算一次
    start = min_required
    for t in range(start, n + 1, step):
        # 參考窗口切片（較早的歷史資料）
        ref_slice = values[t - ref_window - current_window : t - current_window]
        # 當前窗口切片（最近資料）
        current_slice = values[t - current_window : t]

        # 轉換為機率分佈
        p_ref = returns_to_histogram(ref_slice)
        p_current = returns_to_histogram(current_slice)

        # 計算 KL 散度：D_KL(P_current || P_ref)
        # 方向：當前分佈 vs 參考分佈，衡量「當前有多異常」
        kl = compute_kl(p_current, p_ref)

        # 記錄對應的日期（使用窗口最後一天）
        date_idx = min(t - 1, n - 1)
        kl_dates.append(dates[date_idx])
        kl_values.append(kl)

    result = pd.Series(data=kl_values, index=pd.DatetimeIndex(kl_dates), name="kl_divergence")

    return result


def kl_percentile_threshold(kl_series: pd.Series, percentile: float = 95) -> float:
    """
    計算 KL 散度序列在指定百分位數的閾值。

    用於設定警示門檻：例如 95 百分位代表歷史上僅有 5% 的時間
    市場分佈差異超過此閾值，超過時可視為異常信號。

    參數
    ----
    kl_series : pd.Series
        由 rolling_kl() 產生的 KL 散度時間序列。
    percentile : float
        百分位數，範圍 0–100。預設 95。

    回傳
    ----
    float
        對應百分位數的 KL 閾值。
    """
    clean = kl_series.dropna()

    if len(clean) == 0:
        raise ValueError("kl_series 為空，無法計算百分位數閾值。")

    threshold = float(np.percentile(clean.values, percentile))

    return threshold
