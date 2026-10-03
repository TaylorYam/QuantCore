"""Provider re-adjustments and identity checks (#53) — offline, synthetic bars only.

The daily update used to append new bars only; a split after the first
download left the old rows on the pre-split scale (APH 2026-09-03 −48.7%),
and a re-used symbol got another security's bars appended. yfinance is
replaced by in-memory frames everywhere.
"""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from data import price_continuity as pc

DAYS = pd.bdate_range("2026-01-02", periods=120, name="Date")
UPDATE_DAY = str((DAYS[-1] + pd.Timedelta(days=1)).date())


def walk(seed=0, start=100.0, n=len(DAYS)):
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(0, 0.01, n)))


def bars(close, days=DAYS, adj=None, volume=1000, splits=None):
    close = np.asarray(close, float)
    df = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close,
                       "Adj Close": close if adj is None else np.asarray(adj, float),
                       "Volume": volume}, index=pd.DatetimeIndex(days[:len(close)], name="Date"))
    if splits is not None:
        df[pc.SPLIT_COL] = 0.0
        for d, f in splits.items():
            df.loc[pd.Timestamp(d), pc.SPLIT_COL] = f
    return df


def split_case(factor=2.0, at=100, seed=0):
    """True value walk; the local file appended raw bars (old rows on the
    pre-split scale), the provider divides every bar before the ex-date."""
    value = walk(seed)
    raw = np.where(np.arange(len(value)) < at, value, value / factor)
    local = bars(raw)
    provider = bars(value / factor, splits={DAYS[at]: factor})
    return local, provider


# ── compare_history ──────────────────────────────────────────────────────────

def test_identical_history_is_consistent():
    local = bars(walk())
    v = pc.compare_history(local, bars(walk(), splits={}))
    assert v["status"] == "consistent" and not v["steps"] and not v["reasons"]


def test_split_after_the_first_download_is_rescaled():
    local, provider = split_case(2.0, at=100)
    v = pc.compare_history(local, provider)
    assert v["status"] == "rescaled"
    assert v["steps"] == [{"date": str(DAYS[100].date()), "ratio": 2.0,
                           "split_records": [[str(DAYS[100].date()), 2.0]]}]
    assert v["nonstandard_factors"] == []


def test_reverse_split_is_rescaled():
    local, provider = split_case(1 / 3, at=60)
    v = pc.compare_history(local, provider)
    assert v["status"] == "rescaled"
    assert v["steps"][0]["ratio"] == pytest.approx(1 / 3, rel=1e-6)


def test_split_after_the_last_local_bar_is_pending():
    value = walk()
    local = bars(value[:100])
    provider = bars(np.r_[value[:100], value[100:]] / 4, splits={DAYS[100]: 4.0})
    v = pc.compare_history(local, provider)
    assert v["status"] == "rescaled" and v["steps"] == []
    assert v["pending_splits"] == [[str(DAYS[100].date()), 4.0]]


def test_split_record_one_session_after_the_step_still_explains_it():
    # BDX 2026: price drop on 02-09, provider record dated 02-10 (factor 1.272)
    local, provider = split_case(1.272, at=50)
    provider[pc.SPLIT_COL] = 0.0
    provider.loc[DAYS[51], pc.SPLIT_COL] = 1.272
    v = pc.compare_history(local, provider)
    assert v["status"] == "rescaled"
    assert v["nonstandard_factors"] == [[str(DAYS[51].date()), 1.272]]


def test_step_without_split_record_is_refused():
    local, provider = split_case(2.0, at=80)
    provider[pc.SPLIT_COL] = 0.0
    v = pc.compare_history(local, provider)
    assert v["status"] == "mismatch"
    assert "without a matching provider split record" in v["reasons"][0]


def test_another_security_under_the_same_symbol_is_refused():
    v = pc.compare_history(bars(walk(0)), bars(walk(7, start=40.0), splits={}))
    assert v["status"] == "mismatch"


def test_provider_history_shorter_than_local_is_refused():
    # AVB/BBBY/NFX: the symbol now resolves to a security that started later
    local = bars(walk())
    v = pc.compare_history(local, bars(walk()[90:], days=DAYS[90:], splits={}))
    assert v["status"] == "mismatch"
    assert v["reasons"][0].startswith("provider lacks 90 local sessions")


def test_two_decimal_history_on_a_low_price_is_noise_not_a_step():
    # NVDA/GME: migrated history rounded to cents at ~0.4 after later splits
    value = walk(start=0.45)
    v = pc.compare_history(bars(np.round(value, 2)), bars(value, splits={}))
    assert v["status"] == "consistent"


def test_a_revised_last_bar_is_not_a_rescale():
    value = walk()
    revised = value.copy()
    revised[-1] *= 1.012
    v = pc.compare_history(bars(value), bars(revised, splits={}))
    assert v["status"] == "consistent" and v["outliers"] == 1


def revised_tail(factors, start=21.0, seed=0):
    """SMN 2026-09 (#120): the daily update stored provisional bars of a thin
    ETF; the provider later revised the latest ones (local / provider below)."""
    value = walk(seed, start=start)
    local = value.copy()
    local[len(value) - len(factors):] *= np.asarray(factors, float)
    return bars(local), bars(value, splits={})


def test_provisional_recent_bars_revised_by_the_provider_are_replaced():
    local, provider = revised_tail([20.40 / 20.34, 20.3349 / 20.43, 20.85 / 20.92])
    v = pc.compare_history(local, provider)
    assert v["status"] == "rescaled" and v["reasons"] == [] and v["steps"] == []
    assert v["revised_bars"] == [str(d.date()) for d in DAYS[-3:]]
    assert v["identity_mismatch"] is False


def test_an_earlier_revised_bar_next_to_the_revised_tail_is_still_a_revision():
    # thin ETFs revise several provisional bars close together
    local, provider = revised_tail([1.004, 1, 1, 1, 20.40 / 20.34, 20.3349 / 20.43, 20.85 / 20.92])
    v = pc.compare_history(local, provider)
    assert v["status"] == "rescaled" and v["reasons"] == []
    assert v["revised_bars"] == [str(d.date()) for d in (DAYS[-7], *DAYS[-3:])]


def test_revised_recent_bars_beyond_a_thin_name_revision_are_refused():
    local, provider = revised_tail([1.08, 1.09, 1.08])
    v = pc.compare_history(local, provider)
    assert v["status"] == "mismatch" and v["revised_bars"] == []


def test_revision_deeper_than_the_recent_bars_is_still_a_step():
    local, provider = revised_tail([1 / 1.005] * 8)
    v = pc.compare_history(local, provider)
    assert v["status"] == "mismatch" and v["revised_bars"] == []
    assert "without a matching provider split record" in v["reasons"][0]


def test_unrecorded_rescale_of_all_but_the_latest_bars_is_refused():
    # the provider moved every bar before the last three (no split record):
    # the history before the recent bars disagrees, so it is not a revision
    value = walk()
    raw = np.where(np.arange(len(value)) < len(value) - 3, value, value / 1.02)
    v = pc.compare_history(bars(raw), bars(value / 1.02, splits={}))
    assert v["status"] == "mismatch" and v["revised_bars"] == []


def test_identity_doubt_only_on_identity_evidence():
    other = pc.compare_history(bars(walk(0)), bars(walk(7, start=40.0), splits={}))
    assert other["identity_mismatch"] is True
    shorter = pc.compare_history(bars(walk()), bars(walk()[90:], days=DAYS[90:], splits={}))
    assert shorter["identity_mismatch"] is True
    local, provider = split_case(2.0, at=80)
    provider[pc.SPLIT_COL] = 0.0
    step = pc.compare_history(local, provider)
    assert step["status"] == "mismatch" and step["identity_mismatch"] is False


def test_local_adj_close_that_jumped_ahead_of_its_split_is_replaced():
    # BKNG 2026-03-24: appended Adj Close already /25 two weeks before the split
    value = walk(start=5000.0)
    raw = np.where(np.arange(len(value)) < 100, value, value / 25)
    adj = np.where((np.arange(len(value)) >= 90) & (np.arange(len(value)) < 100), raw / 25, raw)
    v = pc.compare_history(bars(raw, adj=adj), bars(value / 25, splits={DAYS[100]: 25.0}))
    assert v["status"] == "rescaled"
    assert [s["date"] for s in v["adj_steps"]] == [str(DAYS[90].date()), str(DAYS[100].date())]


def test_provider_adj_close_falling_is_not_imported():
    local, provider = split_case(2.0, at=100)
    provider.loc[DAYS[50]:, "Adj Close"] = provider.loc[DAYS[50]:, "Close"] * 0.9
    v = pc.compare_history(local, provider)
    assert v["status"] == "mismatch" and "Adj Close falls" in v["reasons"][0]


def test_provider_a_session_behind_the_local_file_is_consistent():
    # HUBB/UA 2026-09-21: the daily update had the bar, the full history did not
    value = walk()
    v = pc.compare_history(bars(value), bars(value[:-1], splits={}))
    assert v["status"] == "consistent" and v["trailing_local_bars"] == 1
    kept = pc.replacement(bars(value), bars(value[:-1], splits={}))
    assert len(kept) == len(DAYS) and kept["Close"].iloc[-1] == pytest.approx(value[-1])


def test_empty_provider_is_no_data():
    assert pc.compare_history(bars(walk()), pd.DataFrame())["status"] == "no_data"


@pytest.mark.parametrize("factor,standard", [(2.0, True), (1.5, True), (1 / 3, True), (25.0, True),
                                             (1 / 18, True), (1.272, False), (0.9535, False),
                                             (2.793, False)])
def test_standard_split_factors(factor, standard):
    assert pc._standard_factor(factor) is standard


# ── window_check (daily, overlap bars only) ──────────────────────────────────

def test_window_identical_overlap_appends():
    value = walk()
    assert pc.window_check(bars(value[:100]), bars(value[80:110], days=DAYS[80:], splits={})) == []


def test_window_rescaled_overlap_needs_full_check():
    value = walk()
    reasons = pc.window_check(bars(value[:100]), bars(value[80:110] / 2, days=DAYS[80:],
                                                      splits={DAYS[100]: 2.0}))
    assert any("split record" in r for r in reasons)
    assert any("provider Close" in r and "x0.5000" in r for r in reasons)


def test_window_revised_recent_bar_is_not_a_rescale():
    value = walk()
    window = value[80:110].copy()
    window[19] *= 1.04                         # thin-name revision of the latest stored bar
    assert pc.window_check(bars(value[:100]), bars(window, days=DAYS[80:], splits={})) == []


def test_window_without_overlap_needs_full_check():
    reasons = pc.window_check(bars(walk()[:50]), bars(walk(3)[100:], days=DAYS[100:]))
    assert reasons == ["no re-downloaded bar overlaps the local file"]


def test_window_dividend_in_adj_close_is_ignored_but_rebasing_is_not():
    value = walk()
    local = bars(value[:100])
    dividend = bars(value[80:110], adj=value[80:110] * 0.99, days=DAYS[80:])
    rebased = bars(value[80:110], adj=value[80:110] / 25, days=DAYS[80:])
    assert pc.window_check(local, dividend) == []
    assert pc.window_check(local, rebased)[0].startswith("provider Adj Close")


def test_etf_window_check_detects_small_adjusted_close_rebase():
    value = walk()
    local = bars(value[:100])
    adjusted = bars(value[80:110], adj=value[80:110] * 0.995, days=DAYS[80:])

    assert pc.window_check(local, adjusted) == []
    assert pc.window_check(local, adjusted, adj_close_tol=1e-6)[0].startswith(
        "provider Adj Close"
    )


# ── daily update (data/update_data.py) with fake yfinance ────────────────────

@pytest.fixture
def updater(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "data" / "update_data.py"
    spec = importlib.util.spec_from_file_location("market_data_updater_53", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STOCK_DIR", tmp_path / "stocks")
    monkeypatch.setattr(module, "ETF_DIR", tmp_path / "etfs")
    monkeypatch.setattr(module, "ALL_ETFS", ["REW"])
    # no reviewed identity dispositions here (#55's deny-list is tested separately)
    manifest = tmp_path / "identity_dispositions.json"
    manifest.write_text(json.dumps({"schema_version": 1, "rules": [
        {"symbol": "COL", "action": "quarantine", "append": "refuse", "reason": "test fixture"}]}),
        encoding="utf-8")
    monkeypatch.setattr(module, "IDENTITY_MANIFEST_PATH", manifest)
    monkeypatch.setattr(module, "_download_ohlcv",
                        lambda *a, **k: pytest.fail("unexpected full-history download"))
    (tmp_path / "stocks").mkdir()
    (tmp_path / "etfs").mkdir()
    return module


def write(path, df):
    df[pc.PRICE_COLS].to_csv(path, float_format="%.6f")


def serve(monkeypatch, module, frames: dict, full: dict | None = None):
    """yf.download returns the requested window of `frames`; the full-history
    download returns `full` (default: the same frames)."""
    calls = []

    def download(tickers, start, end, **kwargs):
        calls.append((tickers, start, kwargs.get("actions")))
        parts = {t: f[(f.index >= pd.Timestamp(start)) & (f.index < pd.Timestamp(end))]
                 for t, f in frames.items() if t in tickers.split()}
        return pd.concat(parts, axis=1)
    monkeypatch.setattr(module.yf, "download", download)
    full = frames if full is None else full
    monkeypatch.setattr(module, "_download_ohlcv",
                        lambda tk, start, end, **k: full[tk][full[tk].index >= pd.Timestamp(start)])
    return calls


def test_update_appends_only_new_bars_and_keeps_stored_rows(updater, monkeypatch):
    value = walk()
    path = updater.STOCK_DIR / "AAA.csv"
    write(path, bars(value[:110]))
    served = bars(value, splits={})
    served.loc[DAYS[109], "Close"] *= 1.0005    # provider revision of a stored bar
    calls = serve(monkeypatch, updater, {"AAA": served})
    before = path.read_bytes()
    assert all(result["status"] == "updated" for result in updater.update_stocks(today=UPDATE_DAY))
    got = pc.read_csv(path)
    assert len(got) == 120 and got.loc[DAYS[109], "Close"] == pytest.approx(value[109], abs=1e-6)
    assert path.read_bytes().startswith(before)
    assert calls[0][1] == str((DAYS[109] - pd.Timedelta(days=pc.OVERLAP_DAYS)).date())
    assert calls[0][2] is True                   # split records requested


def test_update_replaces_the_file_after_a_split(updater, monkeypatch):
    local, provider = split_case(2.0, at=105)
    path = updater.STOCK_DIR / "APH.csv"
    write(path, local.iloc[:110])
    serve(monkeypatch, updater, {"APH": provider})
    assert all(result["status"] == "updated" for result in updater.update_stocks(today=UPDATE_DAY))
    got = pc.read_csv(path)
    assert len(got) == 120
    assert np.allclose(got["Close"], provider["Close"], atol=1e-6)
    assert pc.compare_history(got, provider)["status"] == "consistent"
    assert not list(updater.STOCK_DIR.glob(".*tmp*"))


def test_update_replaces_the_file_after_a_reverse_split(updater, monkeypatch):
    local, provider = split_case(1 / 3, at=112)
    path = updater.STOCK_DIR / "DD.csv"
    write(path, local.iloc[:110])              # split is after the last stored bar
    serve(monkeypatch, updater, {"DD": provider})
    assert all(result["status"] == "updated" for result in updater.update_stocks(today=UPDATE_DAY))
    assert np.allclose(pc.read_csv(path)["Close"], provider["Close"], atol=1e-6)


def test_update_refuses_a_symbol_that_now_points_at_another_security(updater, monkeypatch):
    path = updater.STOCK_DIR / "NFX.csv"
    write(path, bars(walk(0)[:110]))
    before = path.read_bytes()
    # the provider symbol now resolves to a security listed on DAYS[95]
    serve(monkeypatch, updater, {"NFX": bars(walk(9, start=25.0)[95:], days=DAYS[95:], splits={})})
    result = updater.update_stocks(today=UPDATE_DAY)[0]
    assert result["status"] == "invalid_response"
    assert result["ticker"] == "NFX"
    assert path.read_bytes() == before


def test_update_without_new_bars_does_not_check_or_report(updater, monkeypatch):
    # AVB after the merger: the provider stub disagrees with the stored bars but
    # has nothing newer — no append to protect, so no daily error either
    path = updater.STOCK_DIR / "AVB.csv"
    write(path, bars(walk()[:100]))
    before = path.read_bytes()
    serve(monkeypatch, updater, {"AVB": bars(walk()[:100] / 2.793, splits={DAYS[95]: 2.793})})
    assert updater.update_stocks(today=UPDATE_DAY)[0]["status"] == "no_new_bars"
    assert path.read_bytes() == before


def test_update_refuses_when_the_full_history_download_fails(updater, monkeypatch):
    local, provider = split_case(2.0, at=105)
    path = updater.STOCK_DIR / "KLAC.csv"
    write(path, local.iloc[:110])
    before = path.read_bytes()
    serve(monkeypatch, updater, {"KLAC": provider})
    monkeypatch.setattr(updater, "_download_ohlcv", lambda *a, **k: None)
    result = updater.update_stocks(today=UPDATE_DAY)[0]
    assert result["status"] == "download_failed"
    assert path.read_bytes() == before


def test_etf_split_rescale_is_validated_then_rewritten(updater, monkeypatch):
    value = walk(start=14.0)
    path = updater.ETF_DIR / "REW.csv"
    write(path, bars(value[:110]))
    write(updater.ETF_DIR / "VIX.csv", bars(walk(5, start=17.0)))      # VIX already current
    provider = bars(value * 2, splits={DAYS[112]: 0.5})
    serve(monkeypatch, updater, {"REW": provider})
    results = {result["ticker"]: result for result in updater.update_etfs(today=UPDATE_DAY)}
    assert results["REW"]["status"] == "updated"
    assert np.allclose(pc.read_csv(path)["Close"], provider["Close"], atol=1e-6)
    assert pc.compare_history(pc.read_csv(path), provider)["status"] == "consistent"


def test_etf_small_adjusted_close_rebase_refreshes_full_provider_history(updater, monkeypatch):
    value = walk()
    path = updater.ETF_DIR / "REW.csv"
    local = bars(value[:110])
    write(path, local)
    write(updater.ETF_DIR / "VIX.csv", bars(walk(5, start=17.0)))
    provider = bars(value, adj=value * 0.995)
    serve(monkeypatch, updater, {"REW": provider})

    assert all(result["status"] in {"no_update_required", "updated"}
               for result in updater.update_etfs(today=UPDATE_DAY))

    got = pc.read_csv(path)
    assert len(got) == len(provider)
    assert np.allclose(got["Close"], provider["Close"], atol=1e-6)
    assert np.allclose(got["Adj Close"], provider["Adj Close"], atol=1e-6)


def test_etf_revised_provisional_bars_and_dividend_refresh_the_file(updater, monkeypatch):
    # SMN 2026-09-29 (#120): a dividend re-based Adj Close (full check), and the
    # provider had revised the last three stored bars
    value = walk(start=21.0)
    stored = value[:110].copy()
    stored[-3:] *= [20.40 / 20.34, 20.3349 / 20.43, 20.85 / 20.92]
    path = updater.ETF_DIR / "REW.csv"
    write(path, bars(stored))
    write(updater.ETF_DIR / "VIX.csv", bars(walk(5, start=17.0)))
    provider = bars(value, adj=value * 0.993)
    serve(monkeypatch, updater, {"REW": provider})

    results = {result["ticker"]: result for result in updater.update_etfs(today=UPDATE_DAY)}

    assert results["REW"]["status"] == "updated"
    got = pc.read_csv(path)
    assert len(got) == len(provider)
    assert np.allclose(got["Close"], provider["Close"], atol=1e-6)
    assert pc.compare_history(got, provider)["status"] == "consistent"


def test_unexplained_step_is_refused(updater, monkeypatch):
    local, provider = split_case(2.0, at=100)
    provider[pc.SPLIT_COL] = 0.0
    path = updater.STOCK_DIR / "STP.csv"
    write(path, local.iloc[:110])
    serve(monkeypatch, updater, {"STP": provider})
    before = path.read_bytes()

    result = updater.update_stocks(today=UPDATE_DAY)[0]

    assert result["status"] == "invalid_response"
    assert path.read_bytes() == before


def test_etf_update_refuses_provider_history_without_adjusted_close(updater, monkeypatch):
    path = updater.ETF_DIR / "REW.csv"
    local = bars(walk()[:110])
    write(path, local)
    write(updater.ETF_DIR / "VIX.csv", bars(walk(5, start=17.0)))
    provider = bars(walk()).drop(columns=["Adj Close"])
    serve(monkeypatch, updater, {"REW": provider})
    before = path.read_bytes()

    results = {result["ticker"]: result for result in updater.update_etfs(today=UPDATE_DAY)}

    assert results["REW"]["status"] == "invalid_response"
    assert path.read_bytes() == before


def test_vix_full_refresh_uses_close_when_provider_omits_adjusted_close(updater, monkeypatch):
    path = updater.ETF_DIR / "VIX.csv"
    value = walk()
    local = bars(value[:110])
    write(path, local)
    window = bars(value[80:], adj=value[80:] * 1.002, days=DAYS[80:])
    full = bars(value).drop(columns=["Adj Close"])
    monkeypatch.setattr(updater, "_download_ohlcv",
                        lambda symbol, *a, **k: full if symbol == "^VIX" else None)

    added = updater._store(path, window, kind=updater.INDEX, today=UPDATE_DAY,
                           provider_symbol="^VIX")

    stored = pc.read_csv(path)
    assert added == len(value) - len(local)
    assert np.allclose(stored["Adj Close"], stored["Close"], atol=1e-6)


def test_vix_full_history_check_asks_the_provider_for_the_index_symbol(updater, monkeypatch):
    # #194: the full-history check used the file name; "VIX" is not the CBOE index
    value = walk(start=17.0)
    path = updater.ETF_DIR / "VIX.csv"
    write(path, bars(value[:110], volume=0))
    write(updater.ETF_DIR / "REW.csv", bars(walk()))                     # ETFs already current
    window = bars(value, adj=value * 1.002, volume=0)                   # re-based Adj Close → full check
    full = bars(value, volume=0).drop(columns=["Adj Close"])
    requested = []

    # not serve(): VIX's window and full-history calls both go through _download_ohlcv
    def download(symbol, start, end, **kwargs):
        requested.append(symbol)
        if symbol != "^VIX":
            return None
        frame = full if start == str(DAYS[0].date()) else window
        return frame[frame.index >= pd.Timestamp(start)].copy()
    monkeypatch.setattr(updater, "_download_ohlcv", download)

    results = {result["ticker"]: result for result in updater.update_etfs(today=UPDATE_DAY)}

    assert requested == ["^VIX", "^VIX"]
    assert results["VIX"]["status"] == "updated"
    stored = pc.read_csv(path)
    assert len(stored) == len(value)
    assert np.allclose(stored["Close"], value, atol=1e-6)
    assert np.allclose(stored["Adj Close"], stored["Close"], atol=1e-6)


# ── scan → classify → stage → apply (one-time repair) ────────────────────────

def test_scan_stage_apply_round_trip(tmp_path, monkeypatch):
    old = tmp_path / "old"
    for sub in ("stocks", "etfs", "reference"):
        (old / sub).mkdir(parents=True)
    split_local, split_provider = split_case(2.0, at=100)
    write(old / "stocks" / "APH.csv", split_local)
    write(old / "stocks" / "AAA.csv", bars(walk(1)))
    write(old / "stocks" / "NFX.csv", bars(walk(2)))
    (old / "etfs" / "SPY.csv").write_text("Date,Close\n2026-01-02,1\n")
    provider = {"APH": split_provider, "AAA": bars(walk(1), splits={}),
                "NFX": bars(walk(2)[60:], days=DAYS[60:], splits={})}
    monkeypatch.setattr(pc, "_download", lambda symbols, start, end: {s: provider[s] for s in symbols})
    monkeypatch.setitem(__import__("sys").modules, "yfinance",
                        type("yf", (), {"__version__": "fake"}))

    scan_dir = tmp_path / "scan"
    report = pc.scan(old, scan_dir, end="2026-07-01")
    assert report["rescaled"] == ["APH"] and report["mismatch"] == ["NFX"]
    assert pc.classify(old, scan_dir)["rescaled"] == ["APH"]          # offline re-run

    new = tmp_path / "new"
    manifest = pc.stage(old, new, scan_dir)
    assert manifest["changed"] == ["stocks/APH.csv"]
    with pytest.raises(ValueError):
        pc.stage(old, new, scan_dir)                                    # never reuses NEW
    assert (old / "stocks" / "APH.csv").read_bytes() == pc.frame_bytes(split_local[pc.PRICE_COLS])

    live = tmp_path / "live"
    import shutil
    shutil.copytree(old, live)
    assert pc.apply(live, manifest, tmp_path / "bk", dry_run=True)["deployed"] == []
    record = pc.apply(live, manifest, tmp_path / "bk")
    assert record["problems"] == [] and [d["file"] for d in record["deployed"]] == ["stocks/APH.csv"]
    assert (live / "stocks" / "APH.csv").read_bytes() == (new / "stocks" / "APH.csv").read_bytes()
    assert (tmp_path / "bk" / "stocks" / "APH.csv").read_bytes() == \
        (old / "stocks" / "APH.csv").read_bytes()
    again = pc.apply(live, manifest, tmp_path / "bk2")                 # live no longer OLD
    assert again["problems"] and not (tmp_path / "bk2").exists()
    json.dumps(manifest)
