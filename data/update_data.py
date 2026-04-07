"""
QuantCore — 增量資料更新
每次執行只下載「上次更新日期之後」的新資料，追加到現有 CSV。

用法：
    cd C:/QuantCore-master
    python data/update_data.py          # 更新 ETF + 全部股票
    python data/update_data.py --etf    # 只更新 ETF
    python data/update_data.py --stocks # 只更新個股
"""

import argparse
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import date, timedelta

ROOT      = Path("C:/QuantCore-master/data")
ETF_DIR   = ROOT / "etfs"
STOCK_DIR = ROOT / "stocks"

SECTOR_ETFS = ["XLK","XLF","XLV","XLE","XLI","XLY","XLB","XLP","XLU","XLC","XLRE"]
BENCHMARKS  = ["SPY","SSO","QQQ"]
ALL_ETFS    = SECTOR_ETFS + BENCHMARKS
VIX_TICKER  = "^VIX"

TODAY = str(date.today())


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _last_date(csv_path: Path) -> str:
    """讀取 CSV 最後一個交易日，回傳 YYYY-MM-DD 字串。"""
    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    return str(df.index[-1].date())


def _append(existing_path: Path, new_df: pd.DataFrame) -> int:
    """
    將 new_df 追加到現有 CSV，去重後存回。
    回傳新增行數。
    """
    old = pd.read_csv(existing_path, index_col=0, parse_dates=True)
    combined = pd.concat([old, new_df])
    combined.index = pd.to_datetime(combined.index).normalize()
    combined.index.name = "Date"
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined = combined.dropna(subset=["Close"])
    added = len(combined) - len(old)
    combined.to_csv(existing_path, float_format="%.6f")
    return max(added, 0)


def _extract_ticker(raw: pd.DataFrame, tk: str) -> pd.DataFrame | None:
    """
    從 yf.download() 批次結果中提取單一 ticker 的 OHLCV。
    相容 yfinance < 1.0（raw["Open"][tk]）和 >= 1.0（raw[tk]["Open"] 或 MultiIndex）。
    """
    try:
        cols = ["Open", "High", "Low", "Close", "Volume"]
        if raw.columns.nlevels == 2:
            # MultiIndex columns
            top_level = raw.columns.get_level_values(0).unique()
            if tk in top_level:
                # group_by="ticker": (ticker, field) → raw[tk]
                sub = raw[tk]
                df = pd.DataFrame({c: sub[c] for c in cols if c in sub.columns})
            elif cols[0] in top_level:
                # default: (field, ticker) → raw["Open"][tk]
                df = pd.DataFrame({c: raw[c][tk] for c in cols if c in top_level})
            else:
                return None
        else:
            # flat columns (single ticker download)
            df = pd.DataFrame({c: raw[c] for c in cols if c in raw.columns})

        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        df = df.dropna(subset=["Close"])
        return df if not df.empty else None
    except Exception:
        return None


def _download_ohlcv(ticker: str, start: str, end: str,
                    auto_adjust: bool = False) -> pd.DataFrame | None:
    """下載 OHLCV，失敗回傳 None。"""
    try:
        raw = yf.Ticker(ticker).history(
            start=start, end=end, auto_adjust=auto_adjust
        )
        if raw.empty:
            return None
        raw.index = pd.to_datetime(raw.index).tz_localize(None).normalize()
        raw.index.name = "Date"
        cols = ["Open", "High", "Low", "Close", "Volume"]
        df = pd.DataFrame({c: raw[c] if c in raw.columns else 0 for c in cols})
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df.dropna(subset=["Close"])
        return df if not df.empty else None
    except Exception as e:
        print(f"    ERROR {ticker}: {e}")
        return None


# ── ETF 更新 ──────────────────────────────────────────────────────────────────

def update_etfs():
    print("=" * 55)
    print("ETF 更新")
    print("=" * 55)

    # 一次批量下載所有 ETF
    paths = {tk: ETF_DIR / f"{tk}.csv" for tk in ALL_ETFS if (ETF_DIR / f"{tk}.csv").exists()}
    if not paths:
        print("  找不到 ETF CSV，請先執行 migrate_data.py")
        return

    # 以最舊的 last_date 作為起始（確保全部補齊）
    starts = {tk: _last_date(p) for tk, p in paths.items()}
    earliest = min(starts.values())
    start = str((pd.to_datetime(earliest) + timedelta(days=1)).date())

    if start >= TODAY:
        print(f"  ETF 資料已是最新（{earliest}），無需更新。")
        return

    print(f"  下載區間：{start} → {TODAY}")

    # 批次下載（yfinance >= 1.0 欄位格式：group_by="ticker" → raw[tk] 取子 DataFrame）
    tickers_str = " ".join(ALL_ETFS)
    try:
        raw = yf.download(tickers_str, start=start, end=TODAY,
                          auto_adjust=False, group_by="ticker",
                          progress=False)
    except Exception as e:
        print(f"  批次下載失敗：{e}，改逐一下載")
        raw = None

    for tk in ALL_ETFS:
        csv_path = ETF_DIR / f"{tk}.csv"
        if not csv_path.exists():
            continue
        start_tk = str((pd.to_datetime(starts[tk]) + timedelta(days=1)).date())
        if start_tk >= TODAY:
            print(f"  {tk:6s}  已最新")
            continue

        try:
            if raw is not None:
                df = _extract_ticker(raw, tk)
            else:
                df = _download_ohlcv(tk, start_tk, TODAY)

            if df is None or df.empty:
                print(f"  {tk:6s}  無新資料")
                continue

            added = _append(csv_path, df)
            print(f"  {tk:6s}  +{added} 筆  (至 {str(df.index[-1].date())})")
        except Exception as e:
            print(f"  {tk:6s}  ERROR: {e}")

    # VIX 單獨處理
    vix_path = ETF_DIR / "VIX.csv"
    if vix_path.exists():
        vix_start = str((pd.to_datetime(_last_date(vix_path)) + timedelta(days=1)).date())
        if vix_start < TODAY:
            df = _download_ohlcv(VIX_TICKER, vix_start, TODAY)
            if df is not None and not df.empty:
                df["Volume"] = 0
                added = _append(vix_path, df)
                print(f"  {'VIX':6s}  +{added} 筆  (至 {str(df.index[-1].date())})")
        else:
            print(f"  {'VIX':6s}  已最新")


# ── 個股更新 ──────────────────────────────────────────────────────────────────

def update_stocks(batch_size: int = 50):
    print("=" * 55)
    print("個股更新（yfinance 批次下載）")
    print("=" * 55)

    csvs = sorted(STOCK_DIR.glob("*.csv"))
    if not csvs:
        print("  找不到個股 CSV，請先解壓縮歷史資料。")
        return

    print(f"  共 {len(csvs)} 支個股")

    # 依 last_date 分組，同一天起始的批次一起下
    groups: dict[str, list] = {}
    for p in csvs:
        try:
            ld = _last_date(p)
            start = str((pd.to_datetime(ld) + timedelta(days=1)).date())
            if start >= TODAY:
                continue
            groups.setdefault(start, []).append(p)
        except Exception:
            continue

    if not groups:
        print(f"  個股資料已是最新，無需更新。")
        return

    total_added = 0
    total_updated = 0

    for start, paths in sorted(groups.items()):
        tickers = [p.stem for p in paths]
        print(f"\n  起始 {start}：{len(tickers)} 支")

        # 分批下載（避免一次太多）
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i+batch_size]
            batch_str = " ".join(batch)

            try:
                raw = yf.download(batch_str, start=start, end=TODAY,
                                  auto_adjust=False, group_by="ticker",
                                  progress=False)
            except Exception as e:
                print(f"    批次 {i//batch_size+1} 下載失敗：{e}")
                continue

            for tk in batch:
                csv_path = STOCK_DIR / f"{tk}.csv"
                try:
                    df = _extract_ticker(raw, tk)
                    if df is None or df.empty:
                        continue

                    added = _append(csv_path, df)
                    if added > 0:
                        total_added += added
                        total_updated += 1
                except Exception as e:
                    print(f"    {tk}: {e}")

    print(f"\n  完成：{total_updated} 支個股更新，共 +{total_added} 筆新資料")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QuantCore 增量資料更新")
    parser.add_argument("--etf",    action="store_true", help="只更新 ETF")
    parser.add_argument("--stocks", action="store_true", help="只更新個股")
    args = parser.parse_args()

    do_etf    = args.etf    or (not args.etf and not args.stocks)
    do_stocks = args.stocks or (not args.etf and not args.stocks)

    if do_etf:
        update_etfs()
    if do_stocks:
        update_stocks()

    print("\n資料更新完成。")
