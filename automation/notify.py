"""
Telegram 通知統一模組。

所有 script 共用這裡的 `tg_send` / `tg_send_photo`，避免 5 份複製漂移。
Token / chat_id 由 caller 傳入（之後遷移到 AccountCtx 後可直接傳 ctx 欄位）。

訊息原則（#26）：只發與下單有關的訊息和錯誤；`--account all` 的排程用
`Digest` 把三帳戶合成一則，沒內容就不發。

Usage:
    from notify import tg_send, tg_send_photo

    tg_send("message", token="123:abc", chat_id="456")
    tg_send_photo("/path/to/chart.png", token, chat_id, caption="Daily Equity")

    # 若 caller 想避免每次傳 token/chat_id，可以 curry：
    from functools import partial
    tg = partial(tg_send, token=TG_TOKEN, chat_id=TG_CHAT_ID)
    tg("hello")

    # 多帳戶合併一則
    digest = Digest("調倉 2026-09-15", icon="📤")
    digest.add("Taylor", "送出 2 筆 MOO", ["🟢 BUY 5 XLK"], token=t, chat_id=c)
    digest.footer("✅ 9/15 排程都跑完：…", token=t, chat_id=c)   # 21:31 當日排程總結（#38）
    digest.send()
"""

import html
import logging
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

# Telegram 單則上限 4096 字（解析 HTML 後計算）；以原始字串保守切。
TG_MAX_CHARS = 4000


def tg_send(msg: str, *, token: str, chat_id: str,
            parse_mode: str = 'HTML', timeout: float = 10.0) -> bool:
    """
    發送文字訊息到 Telegram。

    token / chat_id 為空字串時靜默 return（方便尚未設定 telegram 的環境）。
    網路錯誤會被捕捉並寫 warning log，不向上拋例外，
    避免 Telegram 失敗影響主要交易流程。
    """
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id':    chat_id,
            'text':       msg,
            'parse_mode': parse_mode,
        }).encode()
        req = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                log.warning(f"Telegram HTTP {resp.status}")
                return False
        return True
    except Exception as e:
        log.warning(f"Telegram notify failed: {e}")
        return False


def tg_send_photo(photo_path: str, *, token: str, chat_id: str,
                  caption: str = "", timeout: float = 30.0) -> None:
    """
    發送圖片 + 可選 caption 到 Telegram（multipart/form-data）。

    用於 daily_report 發送 equity chart。失敗同樣只寫 warning log。
    """
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        boundary = "----QuantCoreBoundary"
        body = b""

        # chat_id field
        body += f"--{boundary}\r\n".encode()
        body += b"Content-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
        body += f"{chat_id}\r\n".encode()

        # caption field
        if caption:
            body += f"--{boundary}\r\n".encode()
            body += b"Content-Disposition: form-data; name=\"caption\"\r\n\r\n"
            body += f"{caption}\r\n".encode()

        # photo file
        filename = Path(photo_path).name
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode()
        body += b"Content-Type: image/png\r\n\r\n"
        with open(photo_path, 'rb') as f:
            body += f.read()
        body += b"\r\n"

        body += f"--{boundary}--\r\n".encode()

        req = urllib.request.Request(url, data=body, method='POST')
        req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                log.warning(f"Telegram photo HTTP {resp.status}")
    except Exception as e:
        log.warning(f"Telegram photo failed: {e}")


def split_message(msg: str, limit: int = TG_MAX_CHARS) -> list[str]:
    """依行切成多段，每段 ≤ limit 字元。

    以行為單位切，避免把同一行內的 HTML tag 拆開；單行本身超長才硬切。
    """
    if len(msg) <= limit:
        return [msg]
    chunks: list[str] = []
    cur = ""
    for line in msg.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


def error_line(e: BaseException | str) -> str:
    """例外（或錯誤訊息）→ 失敗段落的一行：截 300 字、HTML escape（例外訊息
    常含 `<...>`，沒 escape 會讓 Telegram 拒收整則）。"""
    return html.escape(str(e)[:300])


def order_group_lines(orders: list[dict], *, show_price: bool = False) -> list[str]:
    """送單清單 → BUY、SELL 各一行，例：`🟢 BUY 55 ERY · 12 XLB`。

    orders 是 rebalance / daily check 的 placed dict（symbol / action / qty，
    可選 lmt_price）。show_price=True 時附限價（LMT 單才有意義）。
    """
    def fmt(o: dict) -> str:
        s = f"{int(o['qty'])} {o['symbol']}"
        if show_price and o.get('lmt_price'):
            s += f" @ {float(o['lmt_price']):.2f}"
        return s

    buys = [fmt(o) for o in orders if o['action'] == 'BUY']
    sells = [fmt(o) for o in orders if o['action'] != 'BUY']
    lines = []
    if buys:
        lines.append("🟢 BUY " + " · ".join(buys))
    if sells:
        lines.append("🔴 SELL " + " · ".join(sells))
    return lines


# ── 多帳戶合併一則（#26）──────────────────────────────────────────────

LEVEL_RANK = {"ok": 0, "warn": 1, "alert": 2, "error": 3}
LEVEL_ICON = {"warn": "⚠️", "alert": "🔴", "error": "❌"}


class Digest:
    """同一個排程、多個帳戶的 Telegram 訊息合併成一則。

    `--account all` 的腳本在迴圈裡 `add()` 各帳戶段落，結束時 `send()` 一次：
      - 沒有任何段落 → 不發（沒單、沒異常 = 靜默）
      - 標題 icon 取最嚴重的等級：error ❌ > alert 🔴 > warn ⚠️ > 預設 icon
      - headline / body / level 完全相同的段落合成一段（例：三帳戶同一個
        資料錯位警示 → `Taylor、Rosa、Yvonne`）
      - `note()` 是各段共用的說明行（去重），只在有段落時才出現
      - `footer()` 是整則最後一行（#38：21:31 當日排程總結）：有段落時附在
        最後、等級參與標題 icon；沒有段落時訊息只有這一行
      - 依 (token, chat_id) 分組發送：帳戶若覆寫 telegram 設定也不會送錯群

    body、headline、footer 由 caller 負責 HTML escape（動態錯誤字串務必 escape）。
    """

    def __init__(self, title: str, icon: str = ""):
        self.title = title
        self.icon = icon
        self._notes: list[str] = []
        self._sections: list[dict] = []
        self._footers: list[dict] = []

    def note(self, line: str) -> None:
        if line not in self._notes:
            self._notes.append(line)

    def add(self, name: str, headline: str, body=(), level: str = "ok", *,
            token: str, chat_id: str, on_delivered=None) -> None:
        """on_delivered：這一段所在的訊息確定送達（每一段 tg_send 都成功）
        後才呼叫 — 給「只通知一次」的 caller 記已通知用（#106）。"""
        if level not in LEVEL_RANK:
            raise ValueError(f"unknown digest level: {level!r}")
        self._sections.append({
            "name": name,
            "headline": headline,
            "body": tuple(body),
            "level": level,
            "dest": (token, chat_id),
            "on_delivered": on_delivered,
        })

    def footer(self, line: str, level: str = "ok", *,
               token: str, chat_id: str) -> None:
        if level not in LEVEL_RANK:
            raise ValueError(f"unknown digest level: {level!r}")
        self._footers.append({"line": line, "level": level,
                              "dest": (token, chat_id)})

    def has(self, name: str) -> bool:
        return any(s["name"] == name for s in self._sections)

    def outcomes(self, names=()) -> dict[str, tuple[str, str | None]]:
        """各帳戶這一輪的結果 {name: (level, headline)}（#38 執行紀錄用）。

        帳戶有多個段落時取最嚴重的一段；names 裡沒有段落的帳戶 = ("ok", None)，
        代表跑完、沒有要回報的事。
        """
        out: dict[str, tuple[str, str | None]] = {n: ("ok", None) for n in names}
        for s in self._sections:
            cur = out.get(s["name"])
            if (cur is None or cur[1] is None
                    or LEVEL_RANK[s["level"]] > LEVEL_RANK[cur[0]]):
                out[s["name"]] = (s["level"], s["headline"])
        return out

    def destinations(self) -> list[tuple[str, str]]:
        dests: list[tuple[str, str]] = []
        for item in (*self._sections, *self._footers):
            if item["dest"] not in dests:
                dests.append(item["dest"])
        return dests

    def render(self, dest: tuple[str, str] | None = None) -> str | None:
        """組出訊息；dest=None 表示全部段落。沒有段落也沒有 footer 回 None。"""
        sections = [s for s in self._sections
                    if dest is None or s["dest"] == dest]
        footers = [f for f in self._footers
                   if dest is None or f["dest"] == dest]
        if not sections:
            return "\n".join(f["line"] for f in footers) if footers else None

        groups: list[dict] = []
        for s in sections:
            key = (s["headline"], s["body"], s["level"])
            for g in groups:
                if g["key"] == key:
                    g["names"].append(s["name"])
                    break
            else:
                groups.append({"key": key, "names": [s["name"]]})

        worst = max((item["level"] for item in (*sections, *footers)),
                    key=LEVEL_RANK.__getitem__)
        icon = LEVEL_ICON.get(worst, self.icon)
        title = f"{icon} {self.title}".strip()

        lines = [f"<b>{html.escape(title)}</b>", *self._notes]
        for g in groups:
            headline, body, _ = g["key"]
            names = "、".join(html.escape(n) for n in g["names"])
            lines.append("")
            lines.append(f"<b>{names}</b> {headline}".rstrip())
            lines.extend(body)
        if footers:
            lines.append("")
            lines.extend(f["line"] for f in footers)
        return "\n".join(lines)

    def send(self) -> None:
        """每個 destination 發一則（過長自動依行切段）。不向上拋例外。"""
        for token, chat_id in self.destinations():
            try:
                msg = self.render((token, chat_id))
            except Exception as e:
                log.warning(f"Telegram digest render failed: {e}")
                continue
            if msg is None:
                continue
            delivered = True
            for chunk in split_message(msg):
                delivered = bool(tg_send(chunk, token=token, chat_id=chat_id)) and delivered
            if not delivered:
                continue
            for s in self._sections:
                if s["dest"] == (token, chat_id) and s["on_delivered"]:
                    try:
                        s["on_delivered"]()
                    except Exception as e:
                        log.warning(f"Telegram digest on_delivered failed: {e}")
