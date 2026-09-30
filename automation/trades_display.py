"""Recent Trades shared by the Tk and Qt Panels (#150).

`read_latest_trades` reads the last 7 days of trade_log.xlsx (moved unchanged
from control_panel so the new Panel can use it without importing the Tk Panel).
`trade_row` formats one trade the way the Tk TradesView shows it. No GUI imports;
colors come back as theme key names ("green", "red", "yellow", "text_dim").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ib_fetcher import SR_ETFS

TRADE_FILE = Path(__file__).parent / "trade_log.xlsx"

# (header, Tk label width)
TRADE_COLUMNS = (
    ("Time", 14), ("Symbol", 7), ("Action", 5), ("Qty", 5),
    ("Price", 8), ("Strategy", 10), ("Status", 10),
)
EMPTY_TEXT = "No recent trades (7 days)"


@dataclass(frozen=True)
class TradeRow:
    datetime: str
    time: str
    symbol: str
    action: str
    qty: str
    price: str
    strategy: str
    status: str
    action_color: str
    status_color: str

    def cells(self) -> tuple[str, ...]:
        return (self.time, self.symbol, self.action, self.qty, self.price,
                self.strategy, self.status)

    def colors(self) -> tuple[str, ...]:
        """Theme key of each cell, same order as cells()."""
        return ("text_dim", "text", self.action_color, "text", "text", "text_dim",
                self.status_color)


def trade_row(tr: dict) -> TradeRow:
    dt = tr["datetime"]
    fill_px = tr.get("fill_price", 0)
    return TradeRow(
        datetime=dt,
        time=dt[-8:] if len(dt) >= 8 else dt,
        symbol=tr["symbol"],
        action=tr["action"],
        qty=str(tr["qty"]),
        price=f"${fill_px:.2f}" if fill_px > 0 else "—",
        strategy=tr["strategy"],
        status=f'{tr["status"]}{" [DRY]" if tr["dry_run"] else ""}',
        action_color="red" if tr["action"] == "SELL" else "green",
        status_color="yellow" if "DRY" in tr["status"] else "green",
    )


def read_latest_trades(account_name: str = "", path: Path | None = None) -> list:
    """讀取 trade_log.xlsx 最近 7 天的交易記錄，可按帳戶篩選"""
    trade_file = TRADE_FILE if path is None else path
    try:
        import pandas as pd
        if not trade_file.exists():
            return []
        df = pd.read_excel(trade_file, sheet_name="Trades")
        if df.empty:
            return []
        # 按帳戶篩選
        if account_name and "Account" in df.columns:
            df = df[df["Account"] == account_name]
            if df.empty:
                return []
        # 滾動 7 天（不限筆數）
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=7)
        recent = df[df["Date"] >= cutoff].iloc[::-1]
        trades = []
        for _, row in recent.iterrows():
            sym = str(row["Symbol"])
            raw_strat = str(row.get("Strategy", ""))
            if sym in SR_ETFS:
                strat = "SR"
            elif "SR" in raw_strat and "SP500" not in raw_strat and "Combo" not in raw_strat:
                strat = "SR"
            else:
                strat = "Stocks"
            fill_px = row.get("FillPrice", 0)
            try:
                fill_px = float(fill_px)
            except (ValueError, TypeError):
                fill_px = 0.0
            trades.append({
                "datetime": str(row.get("DateTime", "")),
                "symbol": sym,
                "action": str(row["Action"]),
                "qty": int(row["Qty"]),
                "fill_price": fill_px,
                "strategy": strat,
                "status": str(row.get("FillStatus", "")),
                "dry_run": bool(row.get("DryRun", True)),
            })
        return trades
    except Exception:
        return []
