"""
QuantCore — 多帳戶工具
讀取 accounts.json，合併 config.json 共用設定 + 帳戶覆蓋值。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

BASE_DIR      = Path(__file__).parent
CONFIG_PATH   = BASE_DIR / 'config.json'
ACCOUNTS_PATH = BASE_DIR / 'accounts.json'

# clientId 的規則與用途（role）編號只在 broker（#185）；這裡轉出 role 給既有的
# AccountCtx.from_name(name, ROLE_…) 呼叫端。
from broker import (  # noqa: E402,F401
    ROLE_MANUAL_ORDER, ROLE_MONITOR, ROLE_MOO_CHECK, ROLE_PANEL,
    ROLE_REBALANCE, ROLE_REPORT, ROLE_TAKEOVER,
)


# ── 帳戶能力（#211）────────────────────────────────────────────────────────
# accounts.json 每個帳戶可以寫這些欄位（都可省略）：
#   gateway_dir     Gateway 安裝（＝設定）資料夾；有值才能一鍵啟動，null 明確關掉
#   manual_orders   可以在 Panel 手動下單（缺＝true）
#   ty_check        週二・三 20:05 TY check
#   color           畫面代表色
#   remote_gateway  遠端／易斷線的 Gateway：系統匣跟著它、斷線 10 分鐘一次性重連、
#                   市場窗每 30 分 poll（最多一個帳戶）
# 缺欄位時用下表的現狀值（按名字，不分大小寫）；三台機器的 accounts.json 都補上
# 欄位之後，這張表就可以拿掉。
_LEGACY_CAPABILITIES = {
    "TAYLOR": {"gateway_dir": r"C:\IB-TAYLOR\ibgateway", "ty_check": True,
               "color": "#d97706"},
    "ROSA": {"gateway_dir": r"C:\Jts\ibgateway\1041", "remote_gateway": True,
             "color": "#0284c7"},
    "YVONNE": {"gateway_dir": r"C:\IB-YIYI\ibgateway", "ty_check": True,
               "color": "#9333ea"},
}
_CAPABILITY_DEFAULTS = {"gateway_dir": None, "manual_orders": True, "ty_check": False,
                        "color": None, "remote_gateway": False}


@dataclass(frozen=True)
class AccountCapabilities:
    name: str
    gateway_dir: Path | None
    manual_orders: bool
    ty_check: bool
    color: str | None
    remote_gateway: bool

    @property
    def can_start_gateway(self) -> bool:
        return self.gateway_dir is not None


def capabilities_for(accounts: Iterable[dict]) -> list[AccountCapabilities]:
    """帳戶設定（accounts.json 的每一筆）→ 能力，順序不變。remote_gateway 多於一個就報錯。"""
    caps = []
    for account in accounts:
        name = account["name"]
        fields = {**_CAPABILITY_DEFAULTS, **_LEGACY_CAPABILITIES.get(name.upper(), {})}
        fields.update({k: account[k] for k in _CAPABILITY_DEFAULTS if k in account})
        caps.append(AccountCapabilities(
            name=name,
            gateway_dir=Path(fields["gateway_dir"]) if fields["gateway_dir"] else None,
            manual_orders=bool(fields["manual_orders"]),
            ty_check=bool(fields["ty_check"]),
            color=fields["color"] or None,
            remote_gateway=bool(fields["remote_gateway"]),
        ))
    remote = [c.name for c in caps if c.remote_gateway]
    if len(remote) > 1:
        raise ValueError(f"accounts.json 只能有一個 remote_gateway 帳戶，現在有 {remote}")
    return caps


def load_capabilities() -> list[AccountCapabilities]:
    """accounts.json 每個帳戶的能力（缺欄位＝現狀預設）。"""
    return capabilities_for(load_accounts())


def account_capabilities(name: str) -> AccountCapabilities:
    """某個帳戶的能力；不認得的名字報錯（同 get_account_config）。"""
    caps = load_capabilities()
    for cap in caps:
        if cap.name.upper() == str(name).upper():
            return cap
    raise ValueError(f"Account '{name}' not found in accounts.json. "
                     f"Available: {[c.name for c in caps]}")


def remote_gateway_account(caps: Iterable[AccountCapabilities] | None = None) -> str | None:
    """遠端 Gateway 的帳戶名（現狀是 Rosa）；沒有就 None。"""
    caps = load_capabilities() if caps is None else caps
    return next((c.name for c in caps if c.remote_gateway), None)


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


def get_all_account_configs() -> list[dict]:
    """取得所有帳戶的合併 config，方便 --account all 迴圈使用。"""
    return [get_account_config(a['name']) for a in load_accounts()]
