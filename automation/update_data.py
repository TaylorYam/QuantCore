#!/usr/bin/env python3
"""
QuantCore — 每日收盤後資料更新
美東 16:30（收盤後 30 分鐘）由 Task Scheduler 執行

流程：
  1. 更新 ETF 資料（11 板塊 + SPY/SSO/QQQ/VIX）
  2. 更新 SP500 成分股資料（~500 檔）
  3. 發送 Telegram 通知（成功/失敗）
"""

import json, logging, sys, urllib.request, urllib.parse
from pathlib import Path
from datetime import date, datetime

BASE_DIR = Path(__file__).parent
ROOT_DIR = BASE_DIR.parent          # C:/QuantCore-master
LOG_DIR  = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

# 加入 QuantCore root
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# ── 日誌 ──────────────────────────────────────────────────────────────────────
log_file = LOG_DIR / f"update_{date.today().strftime('%Y%m%d')}.log"
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


# ── Telegram（統一由 notify.py 提供）─────────────────────────────────────
from notify import tg_send as _tg


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    start_ts = datetime.now()
    log.info(f"{'='*60}")
    log.info(f"  QuantCore Data Update  {date.today()}")
    log.info(f"{'='*60}")

    errors = []

    # ── 1. 更新 ETF ──────────────────────────────────────────────────────
    log.info("Step 1: Updating ETFs...")
    try:
        from data.update_data import update_etfs
        update_etfs()
        log.info("ETF update done.")
    except Exception as e:
        log.error(f"ETF update failed: {e}", exc_info=True)
        errors.append(f"ETF: {e}")

    # ── 2. 更新個股 ──────────────────────────────────────────────────────
    log.info("Step 2: Updating stocks...")
    try:
        from data.update_data import update_stocks
        update_stocks()
        log.info("Stock update done.")
    except Exception as e:
        log.error(f"Stock update failed: {e}", exc_info=True)
        errors.append(f"Stocks: {e}")

    elapsed = (datetime.now() - start_ts).seconds

    # ── 3. Telegram 通知 ─────────────────────────────────────────────────
    if errors:
        msg = (
            f"<b>⚠️ QuantCore 資料更新部分失敗</b>\n"
            f"Date: {date.today()}\n"
            f"Errors:\n" + "\n".join(f"  • {e}" for e in errors) +
            f"\n\nElapsed: {elapsed}s"
        )
    else:
        msg = (
            f"<b>✅ QuantCore 資料更新完成</b>\n"
            f"Date: {date.today()}\n"
            f"ETFs + Stocks 已更新至最新收盤價\n"
            f"Elapsed: {elapsed}s"
        )

    _tg(msg, token=TG_TOKEN, chat_id=TG_CHAT_ID)
    log.info(f"Done. Elapsed: {elapsed}s  Errors: {len(errors)}")


if __name__ == '__main__':
    main()
