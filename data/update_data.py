"""
QuantCore — 增量資料更新
每次執行下載「上次更新日期之後」的新資料，追加到現有 CSV。

新資料連同最近 OVERLAP_DAYS 天已存的 K 棒一起下載做比對（#53）：provider 分割、
反分割或以分割記錄的配發會回頭調整整段歷史，只 append 會在分割日留下假跳空；
代碼改指別家證券時，append 會把兩家公司接在同一檔。比對結果：
  - 一致 → 照舊只追加新 K 棒
  - 個股被調整 → 抓完整歷史核對是同一證券後原子替換整檔；核對不過 → 不續寫、回報
  - ETF 原始 OHLC、含息 Adj Close 分欄儲存。ETF overlap 若有原始價格分割或
    Adj Close 重編，先比對完整歷史；證實同一證券後才以 provider 全歷史原子替換。
    證券身分或歷史無法核對時停止續寫並回報。

用法：
    cd C:/QuantCore-master
    python data/update_data.py          # 更新 ETF + 全部股票
    python data/update_data.py --etf    # 只更新 ETF
    python data/update_data.py --stocks # 只更新個股
"""

import argparse
import sys
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import date, timedelta

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:  # python data/update_data.py
    sys.path.insert(0, str(_REPO))
from data.identity_audit import identity_guard_reason  # noqa: E402
from data.price_continuity import (OVERLAP_DAYS, PRICE_COLS, SPLIT_COL,  # noqa: E402
                                   atomic_write, compare_history, frame_bytes,
                                   replacement, window_check)

ROOT      = Path(__file__).resolve().parent.parent / "data"
ETF_DIR   = ROOT / "etfs"
STOCK_DIR = ROOT / "stocks"
IDENTITY_MANIFEST_PATH = ROOT / "identity_dispositions.json"

SECTOR_ETFS = ["XLK","XLF","XLV","XLE","XLI","XLY","XLB","XLP","XLU","XLC","XLRE"]
BENCHMARKS  = ["SPY","SSO","QQQ"]
# Skew LS 用：9 檔 ProShares UltraShort (2x) + SEF/REK (1x)，XLC 直接放空無 inverse ETF
INVERSE_ETFS = ["REW","SEF","RXD","ERY","SIJ","SCC","SMN","SZK","SDP","REK"]
ALL_ETFS    = SECTOR_ETFS + BENCHMARKS + INVERSE_ETFS
VIX_TICKER  = "^VIX"

TODAY = str(date.today())


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _last_date(csv_path: Path) -> str:
    """讀取 CSV 最後一個交易日，回傳 YYYY-MM-DD 字串。"""
    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    return str(df.index[-1].date())


def _first_date(csv_path: Path) -> str:
    """讀取 CSV 最早交易日，供日期限定的 security identity guard 使用。"""
    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    if df.empty:
        raise ValueError("CSV has no rows")
    dates = pd.to_datetime(df.index, errors="raise")
    return str(dates.min().date())


def _next_day(last: str) -> str:
    return str((pd.to_datetime(last) + timedelta(days=1)).date())


def _window_start(last: str) -> str:
    """下載起點：最後一列往前 OVERLAP_DAYS 天，重抓的舊 K 棒用來比對（#53）。"""
    return str((pd.to_datetime(last) - timedelta(days=OVERLAP_DAYS)).date())


def _append(existing_path: Path, new_df: pd.DataFrame) -> int:
    """
    將 new_df 的價格欄位追加到現有 CSV，去重後存回。
    回傳新增行數。
    """
    new_df = new_df[[c for c in PRICE_COLS if c in new_df.columns]]
    old = pd.read_csv(existing_path, index_col=0, parse_dates=True)
    combined = pd.concat([old, new_df])
    combined.index = pd.to_datetime(combined.index).normalize()
    combined.index.name = "Date"
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined = combined.dropna(subset=["Close"])
    added = len(combined) - len(old)
    combined.to_csv(existing_path, float_format="%.6f")
    return max(added, 0)


def _store(path: Path, window: pd.DataFrame, errors: list[str], refetch: bool,
           *, refresh_adjusted_history: bool = False) -> int | None:
    """
    追加 path 最後一列之後的 K 棒；重抓的已存 K 棒顯示 provider 調整過歷史或
    代碼換了證券時（#53）：refetch=True（個股）抓完整歷史核對後整檔替換，
    否則不續寫並回報。回傳新增行數；未寫入回傳 None（原因已加進 errors）。
    """
    tk = path.stem
    local = pd.read_csv(path, index_col=0, parse_dates=True)
    last = local.index[-1]
    if not (window.index > last).any():
        return 0      # nothing to append; stored history is audited by price_continuity scan
    if refresh_adjusted_history and tk not in ("VIX", "^VIX"):
        if "Adj Close" not in window or window["Adj Close"].isna().any():
            errors.append(f"{tk}: provider omitted valid Adj Close; ETF history was not updated")
            return None
    reasons = (window_check(local, window, adj_close_tol=1e-6)
               if refresh_adjusted_history else window_check(local, window))
    if not reasons:
        return _append(path, window[window.index > last])
    why = "; ".join(reasons)
    if not refetch:
        errors.append(f"{tk}: provider 調整了已存的歷史價格，ETF 檔不自動重抓，已停止續寫（{why}）")
        return None
    fetch_errors: list[str] = []
    full = _download_ohlcv(tk, str(local.index[0].date()), TODAY, errors=fetch_errors)
    if full is None:
        detail = "; ".join(fetch_errors) or "empty response"
        errors.append(f"{tk}: 需核對完整歷史（{why}），下載失敗，未續寫（{detail}）")
        return None
    if refresh_adjusted_history and tk in ("VIX", "^VIX"):
        if "Adj Close" not in full:
            full["Adj Close"] = full["Close"]
        full["Adj Close"] = full["Adj Close"].fillna(full["Close"])
    if refresh_adjusted_history and tk not in ("VIX", "^VIX") and (
            "Adj Close" not in full or full["Adj Close"].isna().any()):
        errors.append(f"{tk}: full-history response omitted valid Adj Close; ETF history was not updated")
        return None
    verdict = compare_history(local, full)
    if verdict["status"] == "consistent":
        if refresh_adjusted_history and any("Adj Close" in reason for reason in reasons):
            new = replacement(local, full)
            atomic_write(path, frame_bytes(new))
            print(f"    {tk}: provider updated adjusted-price history; refreshed full history "
                  f"{len(local)}→{len(new)} rows")
            return int((new.index > last).sum())
        return _append(path, full[full.index > last])
    if verdict["status"] == "rescaled":
        new = replacement(local, full)
        atomic_write(path, frame_bytes(new))
        events = [f"{s['date']} x{s['ratio']:g}" for s in verdict["steps"]]
        events += [f"{d} x{f:g}（待生效）" for d, f in verdict["pending_splits"]]
        events += [f"Adj Close {s['date']}" for s in verdict["adj_steps"]]
        odd = [f"{d} x{f:g}" for d, f in verdict["nonstandard_factors"]]
        print(f"    {tk}: provider 調整過歷史（{', '.join(events)}），已換成完整歷史 "
              f"{len(local)}→{len(new)} 列" + (f"；非整數比例待人工確認：{', '.join(odd)}" if odd else ""))
        return int((new.index > last).sum())
    errors.append(f"{tk}: provider 歷史與本地檔核對不過（可能已是別的證券），已停止續寫"
                  f"（{'; '.join(verdict['reasons']) or verdict['status']}）")
    return None


def _extract_ticker(raw: pd.DataFrame, tk: str, errors: list[str] | None = None) -> pd.DataFrame | None:
    """
    從 yf.download() 批次結果中提取單一 ticker 的 OHLCV（與分割紀錄）。
    相容 yfinance < 1.0（raw["Open"][tk]）和 >= 1.0（raw[tk]["Open"] 或 MultiIndex）。
    """
    if raw.empty:
        return None
    try:
        cols = PRICE_COLS + [SPLIT_COL]
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
    except Exception as exc:
        if errors is not None:
            errors.append(f"{tk}: invalid download response: {exc}")
        return None


def _download_ohlcv(ticker: str, start: str, end: str,
                    auto_adjust: bool = False, errors: list[str] | None = None) -> pd.DataFrame | None:
    """下載 OHLCV（與分割紀錄），失敗回傳 None。"""
    try:
        raw = yf.Ticker(ticker).history(
            start=start, end=end, auto_adjust=auto_adjust
        )
        if raw.empty:
            return None
        raw.index = pd.to_datetime(raw.index).tz_localize(None).normalize()
        raw.index.name = "Date"
        cols = PRICE_COLS + [SPLIT_COL]
        df = pd.DataFrame({c: raw[c] for c in cols if c in raw.columns})
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df.dropna(subset=["Close"])
        return df if not df.empty else None
    except Exception as e:
        if errors is not None:
            errors.append(f"{ticker}: download failed: {e}")
        print(f"    ERROR {ticker}: {e}")
        return None


# ── ETF 更新 ──────────────────────────────────────────────────────────────────

def update_etfs() -> list[str]:
    """Return confirmed update errors; empty responses do not prove missing sessions."""
    errors = []
    print("=" * 55)
    print("ETF 更新")
    print("=" * 55)

    # 一次批量下載所有 ETF
    paths = {tk: ETF_DIR / f"{tk}.csv" for tk in ALL_ETFS if (ETF_DIR / f"{tk}.csv").exists()}
    if not paths:
        print("  找不到 ETF CSV，請先執行 migrate_data.py")
        return ["ETF: no existing CSV files"]

    # 以最舊的 last_date 作為起始（確保全部補齊）
    starts = {}
    for tk, path in paths.items():
        try:
            starts[tk] = _last_date(path)
        except Exception as exc:
            errors.append(f"{tk}: unreadable CSV: {exc}")
    errors.extend(f"{tk}: missing CSV" for tk in ALL_ETFS if tk not in paths)
    if not starts:
        return errors
    earliest = min(starts.values())
    start = _window_start(earliest)
    current = _next_day(earliest) >= TODAY

    if current:
        print(f"  ETF 資料已是最新（{earliest}），無需更新。")
        # VIX is checked independently below.

    print(f"  下載區間：{start} → {TODAY}（含最近 {OVERLAP_DAYS} 天已存資料比對）")

    # 批次下載（yfinance >= 1.0 欄位格式：group_by="ticker" → raw[tk] 取子 DataFrame）
    tickers_str = " ".join(ALL_ETFS)
    try:
        raw = None if current else yf.download(tickers_str, start=start, end=TODAY,
                          auto_adjust=False, actions=True, group_by="ticker",
                          progress=False)
    except Exception as e:
        print(f"  批次下載失敗：{e}，改逐一下載")
        raw = None

    for tk in ALL_ETFS:
        csv_path = ETF_DIR / f"{tk}.csv"
        if tk not in starts:
            continue
        if _next_day(starts[tk]) >= TODAY:
            print(f"  {tk:6s}  已最新")
            continue

        try:
            window_start = _window_start(starts[tk])
            if raw is not None:
                df = _extract_ticker(raw, tk, errors)
            else:
                df = _download_ohlcv(tk, window_start, TODAY, errors=errors)

            if df is None or df.empty:
                print(f"  {tk:6s}  無新資料")
                continue

            if "Adj Close" not in df or df["Adj Close"].isna().any():
                errors.append(f"{tk}: provider omitted valid Adj Close; ETF history was not updated")
                print(f"  {tk:6s}  無有效 Adj Close，未更新")
                continue

            added = _store(csv_path, df[df.index >= window_start], errors, refetch=True,
                           refresh_adjusted_history=True)
            if added is None:
                print(f"  {tk:6s}  未續寫（見錯誤）")
            else:
                print(f"  {tk:6s}  +{added} 筆  (至 {str(df.index[-1].date())})")
        except Exception as e:
            errors.append(f"{tk}: {e}")
            print(f"  {tk:6s}  ERROR: {e}")

    # VIX 單獨處理
    vix_path = ETF_DIR / "VIX.csv"
    try:
        if vix_path.exists():
            vix_last = _last_date(vix_path)
            if _next_day(vix_last) < TODAY:
                df = _download_ohlcv(VIX_TICKER, _window_start(vix_last), TODAY, errors=errors)
                if df is not None and not df.empty:
                    if "Adj Close" not in df:
                        df["Adj Close"] = df["Close"]
                    df["Adj Close"] = df["Adj Close"].fillna(df["Close"])
                    df["Volume"] = 0
                    added = _store(vix_path, df, errors, refetch=True,
                                   refresh_adjusted_history=True)
                    if added is not None:
                        print(f"  {'VIX':6s}  +{added} 筆  (至 {str(df.index[-1].date())})")
            else:
                print(f"  {'VIX':6s}  已最新")
        else:
            errors.append("VIX: missing CSV")
    except Exception as exc:
        errors.append(f"VIX: {exc}")
    return errors


# ── 個股更新 ──────────────────────────────────────────────────────────────────

def update_stocks(batch_size: int = 50) -> list[str]:
    """Return confirmed per-ticker errors; historical inactive tickers remain valid."""
    errors = []
    print("=" * 55)
    print("個股更新（yfinance 批次下載）")
    print("=" * 55)

    csvs = sorted(STOCK_DIR.glob("*.csv"))
    if not csvs:
        print("  找不到個股 CSV，請先解壓縮歷史資料。")
        return ["Stocks: no existing CSV files"]

    print(f"  共 {len(csvs)} 支個股")

    # 依下載起點（last_date 往前 OVERLAP_DAYS 天）分組，同一天起始的批次一起下
    groups: dict[str, list] = {}
    for p in csvs:
        guard_reason = identity_guard_reason(p.stem, IDENTITY_MANIFEST_PATH)
        if guard_reason:
            try:
                guard_reason = identity_guard_reason(
                    p.stem, IDENTITY_MANIFEST_PATH,
                    local_first_date=_first_date(p),
                )
            except Exception as exc:
                errors.append(f"{p.stem}: unreadable CSV: {exc}")
                continue
        if guard_reason:
            # This is an expected, reviewed disposition rather than an update
            # failure.  Keep the fail-closed skip visible in the job log, but
            # do not turn every daily run into a Telegram warning.
            print(f"    {p.stem}: {guard_reason}; skipped as reviewed")
            continue
        try:
            ld = _last_date(p)
            if _next_day(ld) >= TODAY:
                continue
            groups.setdefault(_window_start(ld), []).append(p)
        except Exception as exc:
            errors.append(f"{p.stem}: unreadable CSV: {exc}")
            continue

    if not groups:
        print(f"  個股資料已是最新，無需更新。")
        return errors

    total_added = 0
    total_updated = 0

    for start, paths in sorted(groups.items()):
        tickers = [p.stem for p in paths]
        print(f"\n  起始 {start}（含最近 {OVERLAP_DAYS} 天已存資料比對）：{len(tickers)} 支")

        # 分批下載（避免一次太多）
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i+batch_size]
            batch_str = " ".join(batch)

            try:
                raw = yf.download(batch_str, start=start, end=TODAY,
                                  auto_adjust=False, actions=True, group_by="ticker",
                                  progress=False)
            except Exception as e:
                errors.extend(f"{tk}: download failed: {e}" for tk in batch)
                print(f"    批次 {i//batch_size+1} 下載失敗：{e}")
                continue

            for tk in batch:
                csv_path = STOCK_DIR / f"{tk}.csv"
                try:
                    df = _extract_ticker(raw, tk, errors)
                    if df is None or df.empty:
                        print(f"    {tk}: no data returned; freshness unverified")
                        continue

                    added = _store(csv_path, df, errors, refetch=True)
                    if added:
                        total_added += added
                        total_updated += 1
                except Exception as e:
                    errors.append(f"{tk}: {e}")
                    print(f"    {tk}: {e}")

    print(f"\n  完成：{total_updated} 支個股更新，共 +{total_added} 筆新資料")

    return errors


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QuantCore 增量資料更新")
    parser.add_argument("--etf",    action="store_true", help="只更新 ETF")
    parser.add_argument("--stocks", action="store_true", help="只更新個股")
    args = parser.parse_args()

    do_etf    = args.etf    or (not args.etf and not args.stocks)
    do_stocks = args.stocks or (not args.etf and not args.stocks)

    errors = []
    if do_etf:
        errors.extend(update_etfs())
    if do_stocks:
        errors.extend(update_stocks())
    for error in errors:
        print(f"ERROR: {error}")
    print(f"\n資料更新結束：{len(errors)} 個已確認錯誤；完整性請另跑 data.health。")
    raise SystemExit(bool(errors))
