#!/usr/bin/env python3
"""
QuantCore — 永豐期貨每日權益報告
透過 Shioaji API 取得期貨帳戶總權益數，發送 Telegram 通知。

使用前：
  1. pip install shioaji
  2. 在 config.json 填入 sinopac_api_key, sinopac_secret_key,
     sinopac_person_id, sinopac_ca_path, sinopac_ca_password
  3. 可手動執行或加入 Windows Task Scheduler 排程
"""

import json, logging, sys, urllib.request, urllib.parse
from pathlib import Path
from datetime import date, datetime

BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

# ── 日誌 ──────────────────────────────────────────────────────────────────────
log_file = LOG_DIR / f"sinopac_{date.today().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)s  %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
log = logging.getLogger(__name__)

# ── 設定 ──────────────────────────────────────────────────────────────────────
cfg_path = BASE_DIR / 'config.json'
with open(cfg_path, encoding='utf-8') as f:
    cfg = json.load(f)

TG_TOKEN   = cfg.get('telegram_token',   '')
TG_CHAT_ID = cfg.get('telegram_chat_id', '')

SINOPAC_API_KEY    = cfg.get('sinopac_api_key',    '')
SINOPAC_SECRET_KEY = cfg.get('sinopac_secret_key', '')
SINOPAC_PERSON_ID  = cfg.get('sinopac_person_id',  '')
SINOPAC_CA_PATH    = cfg.get('sinopac_ca_path',    '')
SINOPAC_CA_PASSWD  = cfg.get('sinopac_ca_password', '')


# ── Telegram（統一由 notify.py 提供）─────────────────────────────────────
from notify import tg_send as _tg


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    log.info(f"Sinopac futures equity report start — {date.today()}")

    if not SINOPAC_API_KEY or not SINOPAC_SECRET_KEY:
        log.error("Missing sinopac_api_key / sinopac_secret_key in config.json")
        sys.exit(1)

    import shioaji as sj

    api = sj.Shioaji()

    # ── 登入 ──
    log.info("Logging in to Sinopac...")
    api.login(
        api_key=SINOPAC_API_KEY,
        secret_key=SINOPAC_SECRET_KEY,
    )
    log.info("Login successful.")

    # ── 啟用憑證（下單需要，查詢可選但建議啟用）──
    if SINOPAC_CA_PATH and SINOPAC_PERSON_ID:
        try:
            api.activate_ca(
                ca_path=SINOPAC_CA_PATH,
                ca_passwd=SINOPAC_CA_PASSWD,
                person_id=SINOPAC_PERSON_ID,
            )
            log.info("CA activated.")
        except Exception as e:
            log.warning(f"CA activation skipped: {e}")

    try:
        # ── 取得期貨帳戶 ──
        futures_account = None
        for acc in api.list_accounts():
            if acc.account_type == sj.constant.AccountType.Futures:
                futures_account = acc
                break

        if futures_account is None:
            raise RuntimeError("找不到期貨帳戶，請確認 API 權限是否包含期貨")

        log.info(f"Futures account: {futures_account.account_id}")

        # ── 查詢保證金 (margin) ──
        margin = api.margin(futures_account)
        log.info(f"Margin response: {margin}")

        # margin 回傳欄位（shioaji >= 1.0）：
        #   equity        — 權益數（總權益）
        #   excess_margin — 超額保證金
        #   margin_call   — 追繳金額
        #   available_margin — 可用保證金
        #   risk_indicator — 風險指標 (%)

        equity    = float(margin.equity)
        excess    = float(margin.excess_margin)
        available = float(margin.available_margin)
        risk_ind  = float(margin.risk_indicator)

        log.info(f"Equity={equity:,.0f}  Excess={excess:,.0f}  "
                 f"Available={available:,.0f}  Risk={risk_ind:.2f}%")

        # ── 風險指標顏色 ──
        if risk_ind < 25:
            risk_icon = "🔴"
        elif risk_ind < 50:
            risk_icon = "🟡"
        else:
            risk_icon = "🟢"

        # ── 組 Telegram 訊息 ──
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        lines = [
            f"<b>📈 永豐期貨每日權益報告 — {now_str}</b>",
            f"帳號: <b>{futures_account.account_id}</b>",
            "",
            f"總權益數: <b>NT${equity:,.0f}</b>",
            f"超額保證金: NT${excess:,.0f}",
            f"可用保證金: NT${available:,.0f}",
            f"{risk_icon} 風險指標: <b>{risk_ind:.2f}%</b>",
        ]

        # ── 追繳警告 ──
        margin_call = float(margin.margin_call) if hasattr(margin, 'margin_call') else 0
        if margin_call > 0:
            lines.append(f"\n🚨 <b>追繳金額: NT${margin_call:,.0f}</b>")

        msg = "\n".join(lines)
        _tg(msg, token=TG_TOKEN, chat_id=TG_CHAT_ID)
        log.info("Telegram report sent.")

    except Exception as e:
        log.error(f"ERROR: {e}", exc_info=True)
        _tg(f"<b>❌ 永豐期貨權益報告失敗</b>\n{e}",
            token=TG_TOKEN, chat_id=TG_CHAT_ID)
        raise

    finally:
        api.logout()
        log.info("Logged out.")


if __name__ == '__main__':
    main()
