"""
QuantCore — 多帳戶工具
讀取 accounts.json，合併 config.json 共用設定 + 帳戶覆蓋值。
"""

import json
from pathlib import Path

BASE_DIR      = Path(__file__).parent
CONFIG_PATH   = BASE_DIR / 'config.json'
ACCOUNTS_PATH = BASE_DIR / 'accounts.json'

# clientId 分配：每帳戶佔 10 個號碼段
# A: 20-29, B: 30-39, C: 40-49, D: 50-59 ...
_CLIENT_ID_BASE = 20
_CLIENT_ID_STEP = 10

# role offset: rebalance=0, report=1, moo_check=2, control_panel=5,
#              manual_order=6, gw_monitor=7, takeover=8
ROLE_REBALANCE = 0
ROLE_REPORT    = 1
ROLE_MOO_CHECK = 2
ROLE_PANEL     = 5
ROLE_MANUAL_ORDER = 6
ROLE_MONITOR   = 7
# 交給策略接管（#107）自己的連線：接管單不會被排程調倉的「全部取消掛單」取消
ROLE_TAKEOVER  = 8


def load_base_config() -> dict:
    """讀取 config.json（共用設定）"""
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


def load_accounts() -> list[dict]:
    """讀取 accounts.json，回傳帳戶清單"""
    if not ACCOUNTS_PATH.exists():
        return []
    with open(ACCOUNTS_PATH, encoding='utf-8') as f:
        return json.load(f)


def get_account_config(name: str) -> dict:
    """
    合併 config.json + 指定帳戶的覆蓋值。
    帳戶欄位覆蓋 config.json 的同名欄位。
    額外加入 account_name 欄位。
    """
    base = load_base_config()
    accounts = load_accounts()

    acct = None
    for a in accounts:
        if a['name'].upper() == name.upper():
            acct = a
            break

    if acct is None:
        raise ValueError(f"Account '{name}' not found in accounts.json. "
                         f"Available: {[a['name'] for a in accounts]}")

    # 帳戶欄位覆蓋 base config
    merged = {**base}
    merged['account_name'] = acct['name']
    if 'account' in acct:
        merged['account'] = acct['account']
    if 'port' in acct:
        merged['port'] = acct['port']
    # 可覆蓋的其他欄位
    for key in ('host', 'alloc_sr', 'alloc_sp500', 'alloc_skewls', 'dry_run',
                'order_type', 'outside_rth', 'wait_fill_sec',
                'telegram_token', 'telegram_chat_id'):
        if key in acct:
            merged[key] = acct[key]

    # leverage 自動推導：永遠 = alloc_sr + alloc_sp500 + alloc_skewls
    # (避免顯示與實際脫節)。新策略 alloc_skewls 預設 0 → 既有帳戶 leverage 不變。
    merged['leverage'] = round(
        merged.get('alloc_sr', 0) +
        merged.get('alloc_sp500', 0) +
        merged.get('alloc_skewls', 0), 2
    )

    return merged


def telegram_dest(name: str | None = None) -> dict:
    """Telegram 目的地 {"token", "chat_id"}，可直接 `**` 給 tg_send / Digest。

    name=None → config.json 共用設定（系統層級訊息，例如 21:31 當日排程總結）。
    有 name → 該帳戶設定（含覆寫）；帳戶讀不到（名字打錯、accounts.json 壞掉）
    時退回 config.json，錯誤訊息才不會跟著發不出去。都讀不到 → 空字串
    （tg_send 靜默略過）。
    """
    loaders = ([lambda: get_account_config(name)] if name else []) + [load_base_config]
    for load in loaders:
        try:
            cfg = load()
        except Exception:
            continue
        return {"token": cfg.get('telegram_token', ''),
                "chat_id": cfg.get('telegram_chat_id', '')}
    return {"token": '', "chat_id": ''}


def update_account_config(name: str, updates: dict) -> None:
    """
    寫入指定帳戶在 accounts.json 的覆蓋值（其他帳戶保持不變）。
    範例：update_account_config('Taylor', {'alloc_sr': 0.75, 'alloc_sp500': 0.45})
    """
    accounts = load_accounts()
    found = False
    for a in accounts:
        if a['name'].upper() == name.upper():
            a.update(updates)
            found = True
            break
    if not found:
        raise ValueError(f"Account '{name}' not found in accounts.json")

    with open(ACCOUNTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)
        f.write('\n')


def get_client_id(account_name: str, role: int = ROLE_REBALANCE) -> int:
    """
    按帳戶名稱 + 角色分配不衝突的 clientId。
    A=20+role, B=30+role, C=40+role ...
    """
    accounts = load_accounts()
    idx = 0
    for i, a in enumerate(accounts):
        if a['name'].upper() == account_name.upper():
            idx = i
            break
    return _CLIENT_ID_BASE + idx * _CLIENT_ID_STEP + role


def get_all_account_configs() -> list[dict]:
    """取得所有帳戶的合併 config，方便 --account all 迴圈使用。"""
    return [get_account_config(a['name']) for a in load_accounts()]
