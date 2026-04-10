"""
Telegram 通知統一模組。

所有 script 共用這裡的 `tg_send` / `tg_send_photo`，避免 5 份複製漂移。
Token / chat_id 由 caller 傳入（之後遷移到 AccountCtx 後可直接傳 ctx 欄位）。

Usage:
    from notify import tg_send, tg_send_photo

    tg_send("message", token="123:abc", chat_id="456")
    tg_send_photo("/path/to/chart.png", token, chat_id, caption="Daily Equity")

    # 若 caller 想避免每次傳 token/chat_id，可以 curry：
    from functools import partial
    tg = partial(tg_send, token=TG_TOKEN, chat_id=TG_CHAT_ID)
    tg("hello")
"""

import logging
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


def tg_send(msg: str, *, token: str, chat_id: str,
            parse_mode: str = 'HTML', timeout: float = 10.0) -> None:
    """
    發送文字訊息到 Telegram。

    token / chat_id 為空字串時靜默 return（方便尚未設定 telegram 的環境）。
    網路錯誤會被捕捉並寫 warning log，不向上拋例外，
    避免 Telegram 失敗影響主要交易流程。
    """
    if not token or not chat_id:
        return
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
    except Exception as e:
        log.warning(f"Telegram notify failed: {e}")


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
