"""
QuantCore — 一次性回補：填回 data/stocks/*.csv 缺失的 Adj Close 欄位

背景：
    舊版 update_data.py 下載時沒抓 "Adj Close" 欄位，導致 2026-01-30 之後
    新增的資料列該欄位為空。SP500 momentum 策略用 Adj Close 算 adj_slope
    （log-linregress），NaN 會讓所有 ticker 被 MAX_ADJ_SLOPE 過濾掉。

本腳本：
    掃描 data/stocks/ 所有 CSV，找出 Adj Close 為 NaN 的列，用 yfinance
    重抓該區間，只把 Adj Close 欄位填回去（其他欄位不動，避免汙染歷史）。
"""

from pathlib import Path
import pandas as pd
import yfinance as yf

STOCK_DIR = Path(__file__).resolve().parent.parent / "data" / "stocks"
BATCH_SIZE = 50


def collect_missing() -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    """回傳 {ticker: (min_date, max_date)} — 需要回補的區間。"""
    result = {}
    for p in sorted(STOCK_DIR.glob("*.csv")):
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        if "Adj Close" not in df.columns:
            continue
        nan_rows = df[df["Adj Close"].isna()]
        if nan_rows.empty:
            continue
        result[p.stem] = (nan_rows.index.min(), nan_rows.index.max())
    return result


def backfill(tickers_ranges: dict[str, tuple[pd.Timestamp, pd.Timestamp]]) -> None:
    items = list(tickers_ranges.items())
    total = len(items)
    fixed = 0

    for i in range(0, total, BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        tickers = [t for t, _ in batch]
        # 用最早 min_date、最晚 max_date+1 作為下載區間（批次統一）
        start = min(r[0] for _, r in batch)
        end   = max(r[1] for _, r in batch) + pd.Timedelta(days=1)

        print(f"  批次 {i//BATCH_SIZE + 1}/{(total + BATCH_SIZE - 1)//BATCH_SIZE}: "
              f"{len(tickers)} 檔  {start.date()} → {end.date()}")

        try:
            raw = yf.download(" ".join(tickers),
                              start=str(start.date()), end=str(end.date()),
                              auto_adjust=False, group_by="ticker",
                              progress=False)
        except Exception as e:
            print(f"    下載失敗：{e}")
            continue

        for tk in tickers:
            csv_path = STOCK_DIR / f"{tk}.csv"
            try:
                # 取出該 ticker 的 Adj Close
                if raw.columns.nlevels == 2:
                    top_level = raw.columns.get_level_values(0).unique()
                    if tk in top_level and "Adj Close" in raw[tk].columns:
                        adj = raw[tk]["Adj Close"]
                    elif "Adj Close" in top_level:
                        adj = raw["Adj Close"][tk] if tk in raw["Adj Close"].columns \
                                                   else None
                    else:
                        adj = None
                else:
                    adj = raw["Adj Close"] if "Adj Close" in raw.columns else None

                if adj is None or adj.dropna().empty:
                    print(f"    {tk:6s}  下載結果沒 Adj Close，跳過")
                    continue

                adj.index = pd.to_datetime(adj.index).tz_localize(None).normalize()
                adj = adj[~adj.index.duplicated(keep="last")].dropna()

                # 讀原 CSV，把對應日期的 Adj Close 填回
                df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
                mask = df["Adj Close"].isna() & df.index.isin(adj.index)
                if not mask.any():
                    continue
                df.loc[mask, "Adj Close"] = adj.reindex(df.index[mask]).values
                remaining_nan = df["Adj Close"].isna().sum()
                df.to_csv(csv_path, float_format="%.6f")
                filled = int(mask.sum())
                print(f"    {tk:6s}  +{filled} 筆回補  (剩餘 NaN: {remaining_nan})")
                fixed += 1
            except Exception as e:
                print(f"    {tk:6s}  ERROR: {e}")

    print(f"\n完成：{fixed}/{total} 檔已回補。")


if __name__ == "__main__":
    print("=" * 55)
    print("Adj Close 回補工具")
    print("=" * 55)
    missing = collect_missing()
    if not missing:
        print("  所有 CSV 的 Adj Close 都完整，無需回補。")
    else:
        dates_min = min(r[0] for r in missing.values())
        dates_max = max(r[1] for r in missing.values())
        print(f"  {len(missing)} 檔需回補，日期範圍 {dates_min.date()} → {dates_max.date()}")
        backfill(missing)
