"""
AccountCtx — immutable account configuration container.

一個帳戶這次執行要用的設定（accounts.json + config.json 合併後凍結）。排程與
Panel 的函式都明確接收 ctx，模組沒有可變的全域設定。

Why:
    - Thread-safe：凍結的 dataclass，一旦建立無法修改，多帳戶並行零 race
    - 顯式依賴：函式簽章 `f(ib, ctx)` 清楚宣告依賴，讀者不用挖全域變數
    - 可測試：unit test 能直接構造假的 ctx，不用 mock 全域變數

Usage:
    from account_ctx import AccountCtx
    from account_utils import ROLE_REBALANCE

    ctx = AccountCtx.from_name('Taylor', ROLE_REBALANCE)
    with Broker().connect(ctx) as conn:
        nav = conn.nav()
    send_account(ctx.name, msg)          # notify：該帳戶的 Telegram（#207）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from account_utils import get_account_config
from broker import client_id as broker_client_id


@dataclass(frozen=True)
class AccountCtx:
    """
    單一帳戶的完整 runtime config 快照。

    所有欄位皆為 immutable。需要不同帳戶就建立新的 ctx。
    `leverage` 為 computed field：永遠 = alloc_sr + alloc_sp500 + alloc_skewls，
    不接受獨立設定值。詳細語意見 [CONTEXT.md](../CONTEXT.md) 與
    [docs/adr/0001-alloc-skew-factor-exposure.md](../docs/adr/0001-alloc-skew-factor-exposure.md)。
    """
    # ── Account identity ─────────────────────────────────────
    name: str                    # 人類可讀名稱，如 "Taylor"
    account: str                 # IB U-prefix 帳戶號，如 "U17800779"

    # ── IB connection ────────────────────────────────────────
    host: str                    # 通常 "127.0.0.1"
    port: int                    # Gateway API port，如 4001
    client_id: int               # 按 (account × role) 唯一分配

    # ── Trading mode ─────────────────────────────────────────
    dry_run: bool                # True = 模擬不下單
    order_type: str              # "MOO" / "LMT" / "MKT"
    outside_rth: bool            # 是否允許盤外成交
    wait_fill_sec: int           # 等待成交回報秒數

    # ── Strategy allocation ──────────────────────────────────
    alloc_sr: float              # Sector Rotation 分配（0.0-1.0+），long-only
    alloc_sp500: float           # SP500 Momentum 分配，long-only
    alloc_skewls: float          # Sector Skew LS 分配，多空（inverse proxy + XLC direct short）

    # ── Data feed ────────────────────────────────────────────
    price_feed_port: int         # 備援報價 port（有 MD 訂閱的帳戶）

    # ── Computed fields ──────────────────────────────────────
    @property
    def leverage(self) -> float:
        """
        Total leverage = SR + SP500 + Skew LS 分配（顯示用，下單不依賴這個值）。

        注意：Skew LS 透過 Inverse Proxy 用 2x 反向 ETF，IB 端真實 margin 占用
        會略低於此 derived value（α-semantics，見 ADR-0001）。
        """
        return round(self.alloc_sr + self.alloc_sp500 + self.alloc_skewls, 4)

    # ── Factory ──────────────────────────────────────────────
    @classmethod
    def from_name(cls, name: str, role: int) -> 'AccountCtx':
        """
        從 accounts.json + config.json 合併建構。

        Args:
            name: 帳戶名稱（Taylor / Rosa / Yvonne）
            role: ROLE_REBALANCE / ROLE_REPORT / ROLE_MOO_CHECK / ROLE_PANEL /
                  ROLE_MANUAL_ORDER / ROLE_MONITOR
                  用於計算唯一的 client_id，避免多行程間衝突

        Returns:
            Frozen AccountCtx instance
        """
        cfg = get_account_config(name)
        return cls(
            name           = cfg.get('account_name', name),
            account        = cfg.get('account', ''),
            host           = cfg.get('host', '127.0.0.1'),
            port           = int(cfg.get('port', 4001)),
            client_id      = broker_client_id(name, role),
            dry_run        = bool(cfg.get('dry_run', True)),
            order_type     = cfg.get('order_type', 'MOO'),
            outside_rth    = bool(cfg.get('outside_rth', False)),
            wait_fill_sec  = int(cfg.get('wait_fill_sec', 30)),
            alloc_sr       = float(cfg.get('alloc_sr', 0.80)),
            alloc_sp500    = float(cfg.get('alloc_sp500', 0.50)),
            alloc_skewls   = float(cfg.get('alloc_skewls', 0.0)),
            price_feed_port= int(cfg.get('price_feed_port', 4002)),
        )

    def __repr__(self) -> str:
        return (
            f"AccountCtx(name={self.name!r} account={self.account!r} "
            f"port={self.port} cid={self.client_id} "
            f"lev={self.leverage}x dry_run={self.dry_run})"
        )
