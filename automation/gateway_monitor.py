"""
QuantCore — Gateway 連線監測（S9 從 control_panel 抽出，2026-04-16 移除自動重啟）

每個帳戶一條背景 thread，維持一條與 IB Gateway 的長壽命連線。
連線一旦斷掉就發一次通知然後 thread 退出 — **不自動重連、不自動重啟**。
要恢復監測必須重啟 Panel 本身。

為什麼不自動重連？
  - 2026-04-15 事故證明：反覆 connect 失敗會在 Gateway 10.41 留下 CLOSE_WAIT
    socket，累積到 2-3 個 Gateway 的 accept thread 就整個卡死、全帳戶 API dead。
  - 結論：Gateway restart / reconnect 全部改為「手動」——斷了就斷了，使用者
    自己從 Panel 的 Start Gateway 按鈕啟動，完事後重開 Panel。
  - 例外：`restart_account_thread(name)` 提供節制的 on-demand 重連入口（單次
    嘗試、不阻塞、拒絕在已 alive thread 上重複 spawn），由 ROSA 30 分輪詢呼叫；
    其節制節奏與 2026-04-15 的『反覆 connect』失敗模式不同。

歷史 (2026-04-15 → 2026-06-01)：原本架構透過 IBC 3.23.0 管理 Taylor / Yvonne
Gateway，但 IBC 在 Gateway 10.41 的 UI scraper 找不到 Auto Restart 控件
（"could not find auto logoff/restart time controls"），任何 IBC 觸發的
auto-restart 路徑（CommandServer RESTART、AutoRestartTime、ReloginAfter…）
都通往那個壞 scraper，沒一個能繞過。

2026-06-01 解法：升級到 Gateway 10.45（UI 把 Auto Restart 加回來），同時
完全棄用 IBC，三個帳戶都改用「個別 Gateway 安裝 + 原生 daily auto-restart」
（multi-install model；見 `gateway_launcher.py` 頂端 docstring）。Sunday
ColdRestart 仍需手動 2FA 一次，這是 IBKR 安全機制不可避免。

設計原則：
  - 事件驅動：connect 成功 / `ib.isConnected()` 變 False 即觸發 callback
  - 所有 IB 連線在 `stop()` 或 thread 退出時清楚 disconnect，避免殭屍 socket
  - Callback 在 worker thread 內被呼叫；GUI consumer 需自己 marshal 回主 thread

Usage
─────
    from gateway_monitor import GatewayMonitor

    def on_connected(name):
        print(f"{name} online")

    def on_disconnected(name, reason):
        print(f"{name} offline: {reason}")   # fired once, no retry notifications

    def on_state_change(name, state):
        # state = "connecting" | "online" | "offline"
        update_ui_dot(name, state)

    mon = GatewayMonitor(
        account_names=[a["name"] for a in load_accounts()],
        on_state_change=on_state_change,
        on_connected=on_connected,
        on_disconnected=on_disconnected,
    )
    mon.start()
    # ...
    mon.stop()
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, Optional

from ib_insync import IB

from account_ctx import AccountCtx
from account_utils import ROLE_MONITOR

log = logging.getLogger(__name__)

# 連線維持迴圈的 poll 週期
POLL_INTERVAL_SEC = 5

# Connect timeout
CONNECT_TIMEOUT_SEC = 10


class GatewayMonitor:
    """
    多帳戶 IB Gateway 連線監測器（單次 connect，無自動重連）。

    每個帳戶啟動一條 daemon thread：
      1. 連一次 Gateway。連上就進 heartbeat 維持迴圈。
      2. 連線斷掉（heartbeat 偵測到 isConnected False，或 connect 直接失敗）
         → 觸發 on_disconnected callback → thread 退出。
      3. thread 退出後不會重啟；要重新監測必須重開 Panel 本身。

    Parameters
    ----------
    account_names : list[str]
        要監測的帳戶名稱清單（每個都會從 accounts.json 解析）
    on_state_change : callable(name, state) | None
        任何狀態變化時都觸發。state ∈ {"connecting", "online", "offline"}
        GUI 用它來更新卡片顏色。
    on_connected : callable(name) | None
        因為不再有重連，這個 callback 實際上不會被觸發（保留簽名以免破壞
        caller 的程式碼；可以傳 None）。
    on_disconnected : callable(name, reason) | None
        連線第一次從 online 變 offline（或首次 connect 失敗）時觸發**一次**。
        同一個 thread 不會再發第二次通知。
    role : int
        連線身分（決定 clientId），預設值班用的 ROLE_MONITOR。
    readonly : bool
        以唯讀 API 連線（不能下單）。

    Callbacks 都在 worker thread 被呼叫 — GUI consumer 需自己 marshal 回主執行緒。
    """

    def __init__(
        self,
        account_names: list[str],
        on_state_change: Optional[Callable[[str, str], None]] = None,
        on_connected: Optional[Callable[[str], None]] = None,
        on_disconnected: Optional[Callable[[str, str], None]] = None,
        *,
        role: int = ROLE_MONITOR,
        readonly: bool = False,
    ):
        self._account_names = list(account_names)
        self._role = role
        self._readonly = readonly
        self._on_state_change = on_state_change
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

        self._stop_flag = False
        self._threads: dict[str, threading.Thread] = {}
        self._last_state: dict[str, str] = {}   # name -> 'init' | 'connecting' | 'online' | 'offline'

    # ── Public API ─────────────────────────────────────────

    def start(self) -> None:
        """啟動所有監測 thread（每帳戶一條，各自只 connect 一次）"""
        self._stop_flag = False
        for name in self._account_names:
            self._last_state[name] = "init"
            t = threading.Thread(target=self._monitor_loop, args=(name,),
                                 daemon=True, name=f"GatewayMonitor-{name}")
            t.start()
            self._threads[name] = t

    def stop(self, timeout: float = 3.0) -> None:
        """
        停止所有監測 thread 並等待收尾。

        - 設 stop flag，heartbeat 迴圈在下一次 `ib.sleep(5)` 檢查後退出
        - 最多等 `timeout` 秒讓 worker thread 自己退出
        """
        self._stop_flag = True
        deadline = time.time() + timeout
        for name, t in list(self._threads.items()):
            remaining = max(0.1, deadline - time.time())
            try:
                t.join(timeout=remaining)
            except Exception:
                pass

    def is_online(self, name: str) -> bool:
        return self._last_state.get(name) == "online"

    def get_state(self, name: str) -> str:
        """回傳目前狀態：'init' | 'connecting' | 'online' | 'offline'"""
        return self._last_state.get(name, "init")

    def restart_account_thread(self, name: str) -> bool:
        """β recovery primitive: re-spawn the worker thread for `name` if the
        previous one has died (or was never started). Refuses if the existing
        thread is still alive — we never want two `_monitor_loop`s for the
        same account fighting over the same client_id.

        Returns True iff a fresh thread was spawned. Returns immediately;
        does NOT wait for connect to complete — the caller is expected to
        sleep a few seconds then poll `is_online(name)`.
        """
        existing = self._threads.get(name)
        if existing is not None and existing.is_alive():
            return False
        # Reset state so the new thread starts from a clean slate.
        self._last_state[name] = "init"
        t = threading.Thread(target=self._monitor_loop, args=(name,),
                             daemon=True, name=f"GatewayMonitor-{name}")
        self._threads[name] = t
        t.start()
        return True

    # ── Worker thread：單一帳戶的監測迴圈（一次 connect，不重試）──

    def _monitor_loop(self, name: str) -> None:
        """背景 thread：連一次 Gateway，維持 heartbeat，斷了就通知後退出。"""
        asyncio.set_event_loop(asyncio.new_event_loop())

        ib = IB()
        try:
            # 每次 start 都重建 ctx，讓 accounts.json 改動能被套用
            ctx = AccountCtx.from_name(name, self._role)

            self._set_state(name, "connecting")
            ib.connect(ctx.host, ctx.port, clientId=ctx.client_id,
                       timeout=CONNECT_TIMEOUT_SEC, readonly=self._readonly)

            self._on_connected_event(name)

            # 連線維持：ib.sleep 驅動 event loop，斷線時 isConnected 變 False
            while ib.isConnected() and not self._stop_flag:
                ib.sleep(POLL_INTERVAL_SEC)

            # 跳出迴圈：不是 stop 就是斷線
            if not self._stop_flag:
                self._on_disconnected_event(name, "connection lost")
        except Exception as e:
            err = str(e) or type(e).__name__
            self._on_disconnected_event(name, err)
        finally:
            # ib.disconnect() 是 idempotent — 即使 connect 本身 timeout 也安全。
            # 必須呼叫，否則 ib_insync 底層 TCP socket 會半開 → Gateway 累積
            # CLOSE_WAIT → accept thread 卡死（2026-04-15 事故 root cause）。
            try:
                ib.disconnect()
            except Exception:
                pass

        # thread 結束。**不重啟、不 retry**。
        # 恢復監測需要使用者手動重啟 Panel（本 class 的 start() 會被再呼叫一次）。

    # ── 狀態變化處理 + callback 觸發 ─────────────────────────

    def _set_state(self, name: str, new_state: str) -> None:
        """更新內部狀態 + 觸發 on_state_change callback"""
        self._last_state[name] = new_state
        if self._on_state_change:
            try:
                self._on_state_change(name, new_state)
            except Exception as e:
                log.warning(f"[GatewayMonitor] on_state_change({name}) raised: {e}")

    def _on_connected_event(self, name: str) -> None:
        """連線成功時：更新狀態 + (理論上)觸發 on_connected。
        因為現在不重連，on_connected 實際永遠不會 fire（prev 一定是 'init' 或
        'connecting'，不是 'offline'），但保留判斷避免未來重啟機制回來時行為不對。"""
        prev = self._last_state.get(name, "init")
        self._set_state(name, "online")
        if prev == "offline" and self._on_connected:
            try:
                self._on_connected(name)
            except Exception as e:
                log.warning(f"[GatewayMonitor] on_connected({name}) raised: {e}")

    def _on_disconnected_event(self, name: str, reason: str) -> None:
        """斷線時：更新狀態，只在第一次從 online → offline 時發一次通知。
        (thread 本來就會退出、不會發第二次，這裡的 guard 是多餘保險)"""
        prev = self._last_state.get(name, "init")
        self._set_state(name, "offline")
        if prev != "offline" and self._on_disconnected:
            try:
                self._on_disconnected(name, reason)
            except Exception as e:
                log.warning(f"[GatewayMonitor] on_disconnected({name}) raised: {e}")
