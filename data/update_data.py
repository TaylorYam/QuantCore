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
from typing import NamedTuple

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:  # python data/update_data.py
    sys.path.insert(0, str(_REPO))
from data.identity_audit import identity_guard_reason  # noqa: E402
from data.update_outcomes import UpdateOutcome, outcome  # noqa: E402
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


class Failure(NamedTuple):
    """A ticker the update could not store, already classified (#189).

    `status` is an UpdateOutcome error status; `detail` is for people only and
    is never parsed back into a status."""
    status: str
    detail: str


# How a file's Adj Close is checked and refreshed: stocks keep the long-standing
# append rules; ETFs need the provider's total-return Adj Close; an index (VIX)
# has no distributions or volume, so Adj Close is Close.
STOCK, ETF, INDEX = "stock", "etf", "index"

_ETF_ADJ_MISSING = "omitted valid Adj Close; ETF history was not updated"


def _has_adj_close(df: pd.DataFrame) -> bool:
    return "Adj Close" in df and not df["Adj Close"].isna().any()


def _adj_close_from_close(df: pd.DataFrame) -> None:
    if "Adj Close" not in df:
        df["Adj Close"] = df["Close"]
    df["Adj Close"] = df["Adj Close"].fillna(df["Close"])


def _store(path: Path, window: pd.DataFrame, *, kind: str, today: str,
           symbol: str | None = None) -> int | Failure:
    """
    追加 path 最後一列之後的 K 棒；重抓的已存 K 棒顯示 provider 調整過歷史或
    代碼換了證券時（#53），抓完整歷史核對後整檔替換；核對不過不續寫。
    `symbol` 是向 provider 查完整歷史用的代號，預設同檔名（VIX.csv 要傳 ^VIX，#194）。
    回傳新增行數，或未寫入的 Failure。
    """
    tk = path.stem
    local = pd.read_csv(path, index_col=0, parse_dates=True)
    last = local.index[-1]
    if not (window.index > last).any():
        return 0      # nothing to append; stored history is audited by price_continuity scan
    adjusted = kind != STOCK
    reasons = (window_check(local, window, adj_close_tol=1e-6)
               if adjusted else window_check(local, window))
    if not reasons:
        return _append(path, window[window.index > last])
    why = "; ".join(reasons)
    full = _download_ohlcv(symbol or tk, str(local.index[0].date()), today)
    if not isinstance(full, pd.DataFrame):
        detail = full.detail if isinstance(full, Failure) else "empty response"
        return Failure("download_failed", f"{tk}: 需核對完整歷史（{why}），下載失敗，未續寫（{detail}）")
    if kind == INDEX:
        _adj_close_from_close(full)
    elif kind == ETF and not _has_adj_close(full):
        return Failure("invalid_response", f"{tk}: full-history response {_ETF_ADJ_MISSING}")
    verdict = compare_history(local, full)
    if verdict["status"] == "consistent":
        if adjusted and any("Adj Close" in reason for reason in reasons):
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
        if verdict["revised_bars"]:
            events.append(f"修訂最近 {len(verdict['revised_bars'])} 根 K 棒 "
                          f"{verdict['revised_bars'][0]}~{verdict['revised_bars'][-1]}")
        odd = [f"{d} x{f:g}" for d, f in verdict["nonstandard_factors"]]
        print(f"    {tk}: provider 調整過歷史（{', '.join(events)}），已換成完整歷史 "
              f"{len(local)}→{len(new)} 列" + (f"；非整數比例待人工確認：{', '.join(odd)}" if odd else ""))
        return int((new.index > last).sum())
    doubt = "（可能已是別的證券）" if verdict["identity_mismatch"] else ""
    return Failure("invalid_response", f"{tk}: provider 歷史與本地檔核對不過{doubt}，已停止續寫"
                                       f"（{'; '.join(verdict['reasons']) or verdict['status']}）")


def _extract_ticker(raw: pd.DataFrame, tk: str) -> pd.DataFrame | Failure | None:
    """
    從 yf.download() 批次結果中提取單一 ticker 的 OHLCV（與分割紀錄）。
    相容 yfinance < 1.0（raw["Open"][tk]）和 >= 1.0（raw[tk]["Open"] 或 MultiIndex）。
    None = 沒有這檔的資料（未知，不是失敗）。
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
        return Failure("invalid_response", f"{tk}: invalid download response: {exc}")


def _download_ohlcv(ticker: str, start: str, end: str,
                    auto_adjust: bool = False) -> pd.DataFrame | Failure | None:
    """下載 OHLCV（與分割紀錄）；空回應回傳 None（未知），下載和格式錯誤回傳 Failure。"""
    try:
        raw = yf.Ticker(ticker).history(
            start=start, end=end, auto_adjust=auto_adjust
        )
    except Exception as e:
        print(f"    ERROR {ticker}: {e}")
        return Failure("download_failed", f"{ticker}: download failed: {e}")
    if raw is None or raw.empty:
        return None
    if "Close" not in raw.columns:
        return Failure("invalid_response", f"{ticker}: invalid download response: missing Close column")
    try:
        raw.index = pd.to_datetime(raw.index).tz_localize(None).normalize()
        raw.index.name = "Date"
        cols = PRICE_COLS + [SPLIT_COL]
        df = pd.DataFrame({c: raw[c] for c in cols if c in raw.columns})
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df.dropna(subset=["Close"])
        return df if not df.empty else None
    except Exception as e:
        return Failure("invalid_response", f"{ticker}: invalid download response: {e}")


def _file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _update_file(ticker: str, dataset: str, path: Path,
                 fetched: pd.DataFrame | Failure | None, *, kind: str, today: str,
                 window_start: str | None = None, symbol: str | None = None) -> UpdateOutcome:
    """Store one provider response into `path` and report what happened.

    The single store-and-judge step for ETFs, VIX and stocks (#189). Empty
    provider responses are inconclusive and do not prove missing sessions.
    `symbol` is the provider symbol when it differs from `ticker` (VIX → ^VIX)."""
    if fetched is None:
        result = outcome(ticker, dataset, "inconclusive",
                         "provider returned no rows; freshness unverified")
    elif isinstance(fetched, Failure):
        result = outcome(ticker, dataset, fetched.status, fetched.detail)
    elif kind == ETF and not _has_adj_close(fetched):
        result = outcome(ticker, dataset, "invalid_response", f"provider {_ETF_ADJ_MISSING}")
    else:
        window = fetched
        if kind == INDEX:
            _adj_close_from_close(window)
            window["Volume"] = 0
        if window_start is not None:
            window = window[window.index >= window_start]
        before = _file_signature(path)
        stored = _store(path, window, kind=kind, today=today, symbol=symbol)
        result = _stored_outcome(ticker, dataset, path, before, stored)
        if dataset == "etfs" and not isinstance(stored, Failure):
            print(f"  {ticker:6s}  +{stored} 筆  (至 {str(fetched.index[-1].date())})")
    if result["status"] not in ("updated", "no_new_bars"):
        print(f"  {ticker:6s}  {result['status']}: {result['detail'].removeprefix(f'{ticker}: ')}")
    return result


def _stored_outcome(
    ticker: str,
    dataset: str,
    path: Path,
    before: tuple[int, int],
    stored: int | Failure,
) -> UpdateOutcome:
    if isinstance(stored, Failure):
        return outcome(ticker, dataset, stored.status, stored.detail)
    if stored > 0:
        return outcome(ticker, dataset, "updated", f"added {stored} bar(s)", rows_added=stored)
    try:
        changed = _file_signature(path) != before
    except OSError:
        changed = False
    if changed:
        return outcome(ticker, dataset, "updated", "provider history reconciled; no new bars", rows_added=0)
    return outcome(ticker, dataset, "no_new_bars", "provider response contained no bars after the local endpoint")


# ── ETF 更新 ──────────────────────────────────────────────────────────────────

def update_etfs(today: str | None = None) -> list[UpdateOutcome]:
    """Return one plain-data update outcome per ETF and VIX.

    `today` (YYYY-MM-DD, exclusive download end) defaults to the call date.
    Empty provider responses are inconclusive and do not prove missing sessions.
    """
    today = today or str(date.today())
    results: dict[str, UpdateOutcome] = {}
    print("=" * 55)
    print("ETF 更新")
    print("=" * 55)

    # 一次批量下載所有 ETF
    paths = {tk: ETF_DIR / f"{tk}.csv" for tk in ALL_ETFS if (ETF_DIR / f"{tk}.csv").exists()}
    if not paths:
        print("  找不到 ETF CSV，請先執行 migrate_data.py")
        results["ETF"] = outcome("ETF", "etfs", "local_data_error", "no existing CSV files")

    # 以最舊的 last_date 作為起始（確保全部補齊）
    starts = {}
    for tk, path in paths.items():
        try:
            starts[tk] = _last_date(path)
        except Exception as exc:
            results[tk] = outcome(tk, "etfs", "local_data_error", f"unreadable CSV: {exc}")
    for tk in ALL_ETFS:
        if tk not in paths and "ETF" not in results:
            results[tk] = outcome(tk, "etfs", "local_data_error", "missing CSV")

    earliest = min(starts.values()) if starts else None
    start = _window_start(earliest) if earliest else None
    current = earliest is None or _next_day(earliest) >= today

    if current and earliest:
        print(f"  ETF 資料已是最新（{earliest}），無需更新。")
        # VIX is checked independently below.

    if start:
        print(f"  下載區間：{start} → {today}（含最近 {OVERLAP_DAYS} 天已存資料比對）")

    # 批次下載（yfinance >= 1.0 欄位格式：group_by="ticker" → raw[tk] 取子 DataFrame）
    tickers_str = " ".join(ALL_ETFS)
    raw = None
    if starts and not current:
        try:
            raw = yf.download(tickers_str, start=start, end=today,
                              auto_adjust=False, actions=True, group_by="ticker",
                              progress=False)
        except Exception as e:
            print(f"  批次下載失敗：{e}，改逐一下載")

    for tk in ALL_ETFS:
        csv_path = ETF_DIR / f"{tk}.csv"
        if tk not in starts:
            continue
        if _next_day(starts[tk]) >= today:
            print(f"  {tk:6s}  已最新")
            results[tk] = outcome(tk, "etfs", "no_update_required", f"local data already reaches {starts[tk]}")
            continue

        try:
            window_start = _window_start(starts[tk])
            fetched = (_extract_ticker(raw, tk) if raw is not None
                       else _download_ohlcv(tk, window_start, today))
            results[tk] = _update_file(tk, "etfs", csv_path, fetched, kind=ETF, today=today,
                                       window_start=window_start)
        except Exception as e:
            print(f"  {tk:6s}  ERROR: {e}")
            results[tk] = outcome(tk, "etfs", "update_failed", str(e))

    # VIX 單獨處理
    vix_path = ETF_DIR / "VIX.csv"
    try:
        if vix_path.exists():
            vix_last = _last_date(vix_path)
            if _next_day(vix_last) < today:
                fetched = _download_ohlcv(VIX_TICKER, _window_start(vix_last), today)
                if isinstance(fetched, Failure):
                    # reported under the file's name, not the provider symbol
                    fetched = fetched._replace(detail=fetched.detail.removeprefix(f"{VIX_TICKER}: "))
                results["VIX"] = _update_file("VIX", "etfs", vix_path, fetched, kind=INDEX, today=today,
                                              symbol=VIX_TICKER)
            else:
                print(f"  {'VIX':6s}  已最新")
                results["VIX"] = outcome("VIX", "etfs", "no_update_required",
                                         f"local data already reaches {vix_last}")
        else:
            results["VIX"] = outcome("VIX", "etfs", "local_data_error", "missing CSV")
    except Exception as exc:
        results["VIX"] = outcome("VIX", "etfs", "local_data_error", str(exc))
    return [results[tk] for tk in [*ALL_ETFS, "VIX", "ETF"] if tk in results]


# ── 個股更新 ──────────────────────────────────────────────────────────────────

def update_stocks(batch_size: int = 50, today: str | None = None) -> list[UpdateOutcome]:
    """Return one structured outcome per local ticker; empty responses stay inconclusive.

    `today` (YYYY-MM-DD, exclusive download end) defaults to the call date."""
    today = today or str(date.today())
    results: dict[str, UpdateOutcome] = {}
    print("=" * 55)
    print("個股更新（yfinance 批次下載）")
    print("=" * 55)

    csvs = sorted(STOCK_DIR.glob("*.csv"))
    if not csvs:
        print("  找不到個股 CSV，請先解壓縮歷史資料。")
        return [outcome("Stocks", "stocks", "local_data_error", "no existing CSV files")]

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
                results[p.stem] = outcome(p.stem, "stocks", "local_data_error", f"unreadable CSV: {exc}")
                continue
        if guard_reason:
            # This is an expected, reviewed disposition rather than an update
            # failure.  Keep the fail-closed skip visible in the job log, but
            # do not turn every daily run into a Telegram warning.
            print(f"    {p.stem}: {guard_reason}; skipped as reviewed")
            results[p.stem] = outcome(p.stem, "stocks", "identity_guard_hold", guard_reason)
            continue
        try:
            ld = _last_date(p)
            if _next_day(ld) >= today:
                results[p.stem] = outcome(p.stem, "stocks", "no_update_required",
                                          f"local data already reaches {ld}")
                continue
            groups.setdefault(_window_start(ld), []).append(p)
        except Exception as exc:
            results[p.stem] = outcome(p.stem, "stocks", "local_data_error", f"unreadable CSV: {exc}")
            continue

    if not groups:
        print(f"  個股資料已是最新，無需更新。")
        return [results[p.stem] for p in csvs]

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
                raw = yf.download(batch_str, start=start, end=today,
                                  auto_adjust=False, actions=True, group_by="ticker",
                                  progress=False)
            except Exception as e:
                print(f"    批次 {i//batch_size+1} 下載失敗：{e}")
                for tk in batch:
                    results[tk] = outcome(tk, "stocks", "download_failed", f"download failed: {e}")
                continue

            for tk in batch:
                csv_path = STOCK_DIR / f"{tk}.csv"
                try:
                    results[tk] = _update_file(tk, "stocks", csv_path, _extract_ticker(raw, tk),
                                               kind=STOCK, today=today)
                    if results[tk]["status"] == "updated":
                        total_added += results[tk]["rows_added"]
                        total_updated += 1
                except Exception as e:
                    print(f"    {tk}: {e}")
                    results[tk] = outcome(tk, "stocks", "update_failed", str(e))

    print(f"\n  完成：{total_updated} 支個股更新，共 +{total_added} 筆新資料")

    return [results[p.stem] for p in csvs]


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QuantCore 增量資料更新")
    parser.add_argument("--etf",    action="store_true", help="只更新 ETF")
    parser.add_argument("--stocks", action="store_true", help="只更新個股")
    args = parser.parse_args()

    do_etf    = args.etf    or (not args.etf and not args.stocks)
    do_stocks = args.stocks or (not args.etf and not args.stocks)

    results = []
    if do_etf:
        results.extend(update_etfs())
    if do_stocks:
        results.extend(update_stocks())
    from data.update_outcomes import outcome_errors
    errors = outcome_errors(results)
    for error in errors:
        print(f"ERROR: {error}")
    print(f"\n資料更新結束：{len(errors)} 個已確認錯誤；完整性請另跑 data.health。")
    raise SystemExit(bool(errors))
