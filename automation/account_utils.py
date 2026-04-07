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

# role offset: rebalance=0, report=1, moo_check=2, control_panel=5
ROLE_REBALANCE = 0
ROLE_REPORT    = 1
ROLE_MOO_CHECK = 2
ROLE_PANEL     = 5


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
    if 'leverage' in acct:
        merged['leverage'] = acct['leverage']
    # 可覆蓋的其他欄位
    for key in ('host', 'alloc_sr', 'alloc_sp500', 'dry_run',
                'order_type', 'outside_rth', 'wait_fill_sec',
                'telegram_token', 'telegram_chat_id'):
        if key in acct:
            merged[key] = acct[key]

    return merged


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
