"""
QuantCore — Shared Edition 資料更新
====================================
透過 yfinance 下載最新收盤資料，無需 IB 連線或 Telegram 設定。

用法：
    cd C:/QuantCore-master
    python -m shared.update_data              # 更新全部（ETF + 個股）
    python -m shared.update_data --etf        # 只更新 ETF
    python -m shared.update_data --stocks     # 只更新個股

建議每個交易日美股收盤後執行一次（台灣時間約早上 5:00 後）。
"""

import argparse
import sys
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    parser = argparse.ArgumentParser(
        description="QuantCore Shared — Data Update (via yfinance)",
    )
    parser.add_argument("--etf",    action="store_true", help="Only update ETFs")
    parser.add_argument("--stocks", action="store_true", help="Only update stocks")
    args = parser.parse_args()

    do_etf    = args.etf    or (not args.etf and not args.stocks)
    do_stocks = args.stocks or (not args.etf and not args.stocks)

    print("\n" + "=" * 55)
    print(f"  QuantCore — Data Update (Shared Edition)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    from data.update_data import update_etfs, update_stocks

    if do_etf:
        update_etfs()

    if do_stocks:
        update_stocks()

    print("\nData update complete.")


if __name__ == "__main__":
    main()
