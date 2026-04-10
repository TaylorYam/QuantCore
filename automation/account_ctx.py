"""
AccountCtx — immutable account configuration container.

取代散落在 rebalance_ib / daily_report / moo_fill_check 的模組全域變數
+ _apply_config() mutation pattern。

Why:
    - Thread-safe：凍結的 dataclass，一旦建立無法修改，多帳戶並行零 race
    - 顯式依賴：函式簽章 `f(ib, ctx)` 清楚宣告依賴，讀者不用挖全域變數
    - 可測試：unit test 能直接構造假的 ctx，不用 mock 全域變數

Usage:
    from account_ctx import AccountCtx
    from account_utils import ROLE_REBALANCE

    ctx = AccountCtx.from_name('Taylor', ROLE_REBALANCE)
    nav = get_nav(ib, ctx)
    tg_send(msg, token=ctx.tg_token, chat_id=ctx.tg_chat_id)

Migration 注意事項：
    - 本 module 不動 _apply_config()，只新增 AccountCtx
    - 後續 S3-S5 slice 會逐步把 callers 從全域變數 migrate 到 ctx
    - S6 slice 才會刪除 _apply_config() 和全域變數
"""

from __future__ import annotations

from dataclasses import dataclass, field

from account_utils import get_account_config, get_client_id


@dataclass(frozen=True)
class AccountCtx:
    """
    單一帳戶的完整 runtime config 快照。

    所有欄位皆為 immutable。需要不同帳戶就建立新的 ctx。
    `leverage` 為 computed field：永遠 = alloc_sr + alloc_sp500，不接受獨立設定值。
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
    alloc_sr: float              # Sector Rotation 分配（0.0-1.0+）
    alloc_sp500: float           # SP500 Momentum 分配

    # ── Data feed ────────────────────────────────────────────
    price_feed_port: int         # 備援報價 port（有 MD 訂閱的帳戶）

    # ── Telegram ─────────────────────────────────────────────
    tg_token: str
    tg_chat_id: str

    # ── Computed fields ──────────────────────────────────────
    @property
    def leverage(self) -> float:
        """Total leverage = SR 分配 + SP500 分配（顯示用，下單不依賴這個值）"""
        return round(self.alloc_sr + self.alloc_sp500, 4)

    # ── Factory ──────────────────────────────────────────────
    @classmethod
    def from_name(cls, name: str, role: int) -> 'AccountCtx':
        """
        從 accounts.json + config.json 合併建構。

        Args:
            name: 帳戶名稱（Taylor / Rosa / Yvonne）
            role: ROLE_REBALANCE / ROLE_REPORT / ROLE_MOO_CHECK / ROLE_PANEL / ROLE_MONITOR
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
            client_id      = get_client_id(name, role),
            dry_run        = bool(cfg.get('dry_run', True)),
            order_type     = cfg.get('order_type', 'MOO'),
            outside_rth    = bool(cfg.get('outside_rth', False)),
            wait_fill_sec  = int(cfg.get('wait_fill_sec', 30)),
            alloc_sr       = float(cfg.get('alloc_sr', 0.80)),
            alloc_sp500    = float(cfg.get('alloc_sp500', 0.50)),
            price_feed_port= int(cfg.get('price_feed_port', 7497)),
            tg_token       = cfg.get('telegram_token', ''),
            tg_chat_id     = cfg.get('telegram_chat_id', ''),
        )

    def __repr__(self) -> str:
        return (
            f"AccountCtx(name={self.name!r} account={self.account!r} "
            f"port={self.port} cid={self.client_id} "
            f"lev={self.leverage}x dry_run={self.dry_run})"
        )
