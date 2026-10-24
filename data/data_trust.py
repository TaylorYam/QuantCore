"""資料可信度（Data Trust）：「這檔在這個 session 的本地行情能不能信」只有一個回答者（#235）。

    trust(folder, symbol, session) -> Trust(status, reason, last_session)

  - ok       有這個 session 的 bar
  - stale    檔案讀得到，但最後一筆在 session 之前（沒更新到）
  - missing  沒有檔案
  - broken   檔案讀不到、空檔、沒有 Close 欄、或歷史別名界限外沒有 bar

讀檔一律經過 `data.market_data`（同一份讀取契約）。三個地方問它：

  - 04:30 資料更新（automation/update_data）：更新完寫一份結論
    `automation/logs/data_trust/<YYYYMMDD>.json`；成分股或策略 ETF 連續
    `LAGGING_SESSIONS` 個交易日沒有新 bar、缺檔或壞檔，就列進資料更新的失敗通知
    （更新只續寫已有的檔，缺檔不會自己補回來）。
  - SP500 選股（strategies/sp500_momentum/selection）：這次訊號用到、但不可信的檔
    由 21:00 調倉的 Telegram／log 列出（使用者 2026-10-22 選 B：排名照跑、不改送單）。
  - 離線：`python -m data.data_trust --session YYYY-MM-DD [--stocks A,B] [--etfs X,Y]`。

「落後幾個交易日」用 NYSE 官方登錄表（automation/market_calendar）算，不另外記計數器：
最後一筆 bar 之後、到 session 為止有幾個 session 就是落後幾天。漏跑一天也算得對。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from automation import market_calendar as mc  # official NYSE registry（#234）
from data import market_data

OK = "ok"
STALE = "stale"
MISSING = "missing"
BROKEN = "broken"

LAGGING_SESSIONS = 3          # 連續幾個交易日沒有新 bar 就在 04:30 告警（使用者 2026-10-22 定）
SCHEMA = "data_trust/1"
# 04:30 結論的資料夾；測試整個階段指到暫存目錄（conftest）
CONCLUSION_DIR = Path(__file__).resolve().parents[1] / "automation" / "logs" / "data_trust"


@dataclass(frozen=True)
class Trust:
    """一檔在一個 session 的可信度。reason 給人看，不回頭解析。"""
    symbol: str
    status: str
    reason: str = ""
    last_session: Optional[str] = None      # 最後一筆 bar（YYYY-MM-DD）；讀不到檔時為 None

    @property
    def ok(self) -> bool:
        return self.status == OK

    def describe(self) -> str:
        """Telegram／log 用的一小段，例如「PSKY 沒更新（最後 2026-10-16）」。"""
        if self.status == STALE:
            return f"{self.symbol} 沒更新（最後 {self.last_session}）"
        if self.status == MISSING:
            return f"{self.symbol} 缺檔"
        if self.status == BROKEN:
            return f"{self.symbol} 壞檔（{self.reason}）"
        return self.symbol


def _session(session) -> pd.Timestamp:
    return pd.Timestamp(session).normalize()


def judge_frame(symbol: str, frame: pd.DataFrame, session) -> Trust:
    """已讀進來的 bar（index = Date）在 session 可不可信。"""
    sess = _session(session)
    upto = frame.index[frame.index <= sess]
    if len(upto) == 0:
        return Trust(symbol, BROKEN, f"沒有 {sess.date()} 以前的 bar")
    last = upto.max()
    if last < sess:
        return Trust(symbol, STALE, f"最後一筆 {last.date()}", str(last.date()))
    return Trust(symbol, OK, last_session=str(last.date()))


def judge_problem(symbol: str, problem: str) -> Trust:
    """market_data 讀檔失敗的 problem → Trust（缺檔 missing，其餘 broken）。"""
    if problem == market_data.MISSING:
        return Trust(symbol, MISSING, "沒有檔案")
    return Trust(symbol, BROKEN, problem)


def trust(folder: str | Path, symbol: str, session) -> Trust:
    """讀 `folder` 裡這個代碼的行情檔（套 session 的歷史別名），回答可不可信。"""
    sess = _session(session)
    read = market_data.bars(folder, symbol, as_of=sess)
    if not read.ok:
        return judge_problem(symbol, read.problem)
    return judge_frame(symbol, read.frame, sess)


def sessions_behind(t: Trust, session) -> Optional[int]:
    """最後一筆 bar 之後、到 session 為止有幾個 NYSE session；讀不到檔（沒有最後一筆）為 None。"""
    if t.last_session is None:
        return None
    start = date.fromisoformat(t.last_session) + timedelta(days=1)
    end = _session(session).date()
    return len(mc.official_sessions(start, end)) if start <= end else 0


# ── 04:30 的結論 ──────────────────────────────────────────────────────────

def conclude(symbols: dict[str, Iterable[str]], session, *,
             root: str | Path | None = None) -> dict:
    """{子資料夾: 代碼} 逐檔問 trust → 可寫成 JSON 的結論（只列不可信的檔）。

    symbols 例如 {"stocks": 成分股, "etfs": 策略 ETF}；root 預設 market_data.data_root()。
    """
    base = Path(root) if root is not None else market_data.data_root()
    sess = _session(session)
    checked, problems = 0, {}
    for folder, names in symbols.items():
        for symbol in sorted(set(names)):
            checked += 1
            t = trust(base / folder, symbol, sess)
            if not t.ok:
                problems[symbol] = {**asdict(t), "folder": folder,
                                    "sessions_behind": sessions_behind(t, sess)}
    return {"schema": SCHEMA, "session": str(sess.date()), "checked": checked,
            "problems": problems}


def notice_lines(conclusion: dict) -> list[str]:
    """結論裡要列進 04:30 失敗通知的檔，一檔一行（依代碼排序）：缺檔、壞檔，以及連續
    ≥ LAGGING_SESSIONS 個交易日沒有新 bar 的檔；落後不到的照記在結論裡、不告警。"""
    out = []
    for symbol, row in sorted(conclusion.get("problems", {}).items()):
        if row["status"] != STALE:
            label = "缺檔" if row["status"] == MISSING else "壞檔"
            out.append(f"{symbol}: {label}（{row['reason']}）")
        elif (row.get("sessions_behind") or 0) >= LAGGING_SESSIONS:
            out.append(f"{symbol}: 連續 {row['sessions_behind']} 個交易日沒有新 bar"
                       f"（最後 {row['last_session']}）")
    return out


def summary_lines(problems: list[Trust], *, limit: int = 10) -> list[str]:
    """不可信的檔 → 一行一檔（最多 limit 行，其餘併成「…另 N 檔」）。"""
    lines = [f"  • {t.describe()}" for t in problems[:limit]]
    if len(problems) > limit:
        lines.append(f"  • …另 {len(problems) - limit} 檔")
    return lines


# ── 離線報表 ──────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地行情在某個 session 的可信度（唯讀）")
    parser.add_argument("--session", required=True, help="要檢查的 session，YYYY-MM-DD")
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--stocks", default="", help="逗號分隔的個股代碼（預設：stocks/ 全部）")
    parser.add_argument("--etfs", default="", help="逗號分隔的 ETF 代碼（預設：etfs/ 全部）")
    args = parser.parse_args(argv)
    root = args.data_root or market_data.data_root()

    def names(arg: str, folder: str) -> list[str]:
        if arg:
            return [s.strip() for s in arg.split(",") if s.strip()]
        return [p.stem for p in sorted((root / folder).glob("*.csv"))]

    report = conclude({"stocks": names(args.stocks, "stocks"),
                       "etfs": names(args.etfs, "etfs")}, args.session, root=root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(bool(report["problems"]))


if __name__ == "__main__":
    raise SystemExit(main())
