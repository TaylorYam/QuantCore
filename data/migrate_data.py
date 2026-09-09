"""
QuantCore 資料遷移腳本
統一格式：CSV，欄位 Date, Open, High, Low, Close, Volume
來源：
  - ETF/基準：yfinance 重新下載（確保 OHLCV 完整）
  - SP500 成分股：從舊資料夾轉換
  - 參考檔案：直接複製
"""

import os, shutil
import pandas as pd
import yfinance as yf
from pathlib import Path

# ── 路徑設定 ──────────────────────────────────────────────────
ROOT        = Path(__file__).resolve().parent.parent / "data"
ETF_DIR     = ROOT / "etfs"
STOCK_DIR   = ROOT / "stocks"
REF_DIR     = ROOT / "reference"
SP500_SRC   = Path("C:/Users/kelvi/Documents/SPY stock system/data")

START_DATE  = "2015-01-01"

# ── 目標 ETF 清單 ─────────────────────────────────────────────
SECTOR_ETFS = ["XLK","XLF","XLV","XLE","XLI","XLY","XLB","XLP","XLU","XLC","XLRE"]
BENCHMARKS  = ["SPY","SSO","QQQ"]
VIX_TICKER  = "^VIX"

ALL_ETFS    = SECTOR_ETFS + BENCHMARKS  # VIX 單獨處理

# ── 統一格式函數 ──────────────────────────────────────────────
COLS = ["Open", "High", "Low", "Close", "Volume"]

def standardize(df: pd.DataFrame) -> pd.DataFrame:
    """統一欄位、清理索引、排序"""
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "Date"

    # 補齊欄位（VIX 沒有 Volume）
    for c in COLS:
        if c not in df.columns:
            df[c] = 0

    df = df[COLS]
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df.dropna(subset=["Open","Close"])
    return df


def save(df: pd.DataFrame, path: Path):
    df.to_csv(path, float_format="%.6f")


# ════════════════════════════════════════════════════════════════
# PART 1：ETF + 基準（yfinance 重新下載）
# ════════════════════════════════════════════════════════════════
print("=" * 60)
print("PART 1  下載 ETF & 基準（yfinance）")
print(f"  目標：{', '.join(ALL_ETFS)}")

batch = yf.Tickers(ALL_ETFS)
hist  = batch.history(start=START_DATE, auto_adjust=False)
hist.index = pd.to_datetime(hist.index).tz_localize(None).normalize()

ok, fail = [], []
for tk in ALL_ETFS:
    try:
        df = pd.DataFrame({
            "Open":   hist["Open"][tk],
            "High":   hist["High"][tk],
            "Low":    hist["Low"][tk],
            "Close":  hist["Close"][tk],
            "Volume": hist["Volume"][tk],
        })
        df = standardize(df)
        save(df, ETF_DIR / f"{tk}.csv")
        ok.append(tk)
        print(f"  ✓ {tk:6s}  {df.index[0].date()} → {df.index[-1].date()}  ({len(df)} rows)")
    except Exception as e:
        fail.append(tk)
        print(f"  ✗ {tk:6s}  ERROR: {e}")

# VIX 單獨下載（^VIX → VIX.csv）
print("\n  下載 VIX...")
try:
    vix_raw = yf.Ticker(VIX_TICKER).history(start=START_DATE, auto_adjust=False)
    vix_raw.index = pd.to_datetime(vix_raw.index).tz_localize(None).normalize()
    vix = pd.DataFrame({
        "Open":   vix_raw["Open"],
        "High":   vix_raw["High"],
        "Low":    vix_raw["Low"],
        "Close":  vix_raw["Close"],
        "Volume": 0,
    })
    vix = standardize(vix)
    save(vix, ETF_DIR / "VIX.csv")
    print(f"  ✓ VIX    {vix.index[0].date()} → {vix.index[-1].date()}  ({len(vix)} rows)")
    ok.append("VIX")
except Exception as e:
    fail.append("VIX")
    print(f"  ✗ VIX    ERROR: {e}")

print(f"\n  PART 1 完成：{len(ok)} 成功 / {len(fail)} 失敗")
if fail:
    print(f"  失敗清單：{fail}")


# ════════════════════════════════════════════════════════════════
# PART 2：SP500 成分股（從舊資料夾轉換）
# ════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("PART 2  SP500 成分股轉換（.txt → .csv）")

# 排除非股票檔案
SKIP = {"SPY.txt","SSO.txt","QQQ.txt","sp500_constituents_daily.xlsx",
        "us_trading_days.xlsx"}

stock_files = [f for f in SP500_SRC.iterdir()
               if f.suffix == ".txt" and f.name not in SKIP]
stock_files.sort()

ok2, fail2, skip2 = [], [], []

for src_path in stock_files:
    tk = src_path.stem
    dst_path = STOCK_DIR / f"{tk}.csv"

    try:
        # 格式：ticker, date, o, h, l, c, adj_c, vol
        df = pd.read_csv(src_path,
                         header=0,
                         parse_dates=["date"],
                         dayfirst=False)

        df = df.rename(columns={
            "date":  "Date",
            "o":     "Open",
            "h":     "High",
            "l":     "Low",
            "c":     "Close",
            "vol":   "Volume",
        })
        df = df.set_index("Date")
        df = df[["Open","High","Low","Close","Volume"]]   # 丟掉 ticker, adj_c
        df = standardize(df)

        if len(df) < 50:
            skip2.append(tk)
            continue

        save(df, dst_path)
        ok2.append(tk)

    except Exception as e:
        fail2.append((tk, str(e)))

print(f"  轉換完成：{len(ok2)} 成功 / {len(fail2)} 失敗 / {len(skip2)} 跳過（資料不足）")
if fail2:
    print("  失敗清單：")
    for tk, err in fail2[:10]:
        print(f"    {tk}: {err}")
if skip2:
    print(f"  跳過清單：{skip2[:20]}{'...' if len(skip2)>20 else ''}")


# ════════════════════════════════════════════════════════════════
# PART 3：參考檔案複製
# ════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("PART 3  複製參考檔案")

REF_FILES = [
    SP500_SRC / "sp500_constituents_daily.xlsx",
    SP500_SRC / "us_trading_days.xlsx",
]

for src in REF_FILES:
    if src.exists():
        dst = REF_DIR / src.name
        shutil.copy2(src, dst)
        size_mb = dst.stat().st_size / 1024 / 1024
        print(f"  ✓ {src.name}  ({size_mb:.1f} MB)")
    else:
        print(f"  ✗ {src.name}  NOT FOUND")


# ════════════════════════════════════════════════════════════════
# PART 4：驗證摘要
# ════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("PART 4  驗證摘要")

etf_count   = len(list(ETF_DIR.glob("*.csv")))
stock_count = len(list(STOCK_DIR.glob("*.csv")))
ref_count   = len(list(REF_DIR.glob("*")))

print(f"  data/etfs/      : {etf_count} 檔案")
print(f"  data/stocks/    : {stock_count} 檔案")
print(f"  data/reference/ : {ref_count} 檔案")

# 抽查幾個檔案格式
print("\n  格式抽查：")
for check in ["etfs/SPY.csv", "etfs/XLK.csv", "etfs/VIX.csv"]:
    p = ROOT / check
    if p.exists():
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        print(f"  {check:30s}  欄位: {list(df.columns)}  rows: {len(df)}")

# 抽查一支股票
stock_samples = list(STOCK_DIR.glob("*.csv"))
if stock_samples:
    df = pd.read_csv(stock_samples[0], index_col=0, parse_dates=True)
    print(f"  stocks/{stock_samples[0].name:25s}  欄位: {list(df.columns)}  rows: {len(df)}")

print("\n" + "=" * 60)
print("遷移完成！")
print(f"  資料根目錄：{ROOT}/")
print("=" * 60)
