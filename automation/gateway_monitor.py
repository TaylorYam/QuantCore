"""
QuantCore — Gateway 持久連線監測（S9 從 control_panel 抽出）

每個帳戶一條背景 thread，各自維持與 IB Gateway 的長壽命連線。
透過 callback 回傳狀態變化給 GUI（Tk Panel）或任何其他 consumer。

設計原則：
  - 事件驅動（不靠 polling）— connect 成功 / `ib.isConnected()` 變 False 即觸發 callback
  - Reconnect backoff：10s → 30s → 60s → 60s ...
  - 所有 IB 連線在 `stop()` 時清楚 disconnect，避免殭屍 client slot
  - Callback 在 worker thread 內被呼叫；GUI consumer 需自己 marshal 回主 thread
    （通常透過 `tk.Tk.after(0, fn)`）

Usage
─────
    from gateway_monitor import GatewayMonitor

    def on_connected(name):
        print(f"{name} online")

    def on_disconnected(name, reason):
        print(f"{name} offline: {reason}")

    def on_state_change(name, state):
        # state = "connecting" | "online" | "offline"
        update_ui_dot(name, state)

    mon = GatewayMonitor(
        account_names=["Taylor", "Rosa", "Yvonne"],
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

# Reconnect backoff schedule（秒）
# R4.1: 首次失敗用 3 秒快速重試，處理 Panel 啟動時 Gateway 正在 ready 的 race —
#       避免首次 race-loss 就卡進 10→30→60s 長 backoff 讓 UI 誤顯 OFFLINE。
#       第二次以後照原本的 10→30→60→60 schedule。
BACKOFF_SCHEDULE = [3, 10, 30, 60, 60]

# 連線維持迴圈的 poll 週期
POLL_INTERVAL_SEC = 5

# Connect timeout
CONNECT_TIMEOUT_SEC = 10


class GatewayMonitor:
    """
    多帳戶 IB Gateway 持久連線監測器。

    每個帳戶啟動一條 daemon thread，維持與 Gateway 的長壽命 IB 連線。
    連線斷掉時自動 backoff 重連，並透過 callbacks 通知 consumer。

    Parameters
    ----------
    account_names : list[str]
        要監測的帳戶名稱清單（每個都會從 accounts.json 解析）
    on_state_change : callable(name, state) | None
        任何狀態變化時都觸發。state ∈ {"connecting", "online", "offline"}
        無論是不是第一次連上、無論是不是從斷線恢復，每個狀態進入都會呼叫。
        GUI 用它來更新卡片顏色。
    on_connected : callable(name) | None
        每次連線成功時觸發（**不包含第一次 init**）。
        用於「從斷線恢復」才發的通知。
    on_disconnected : callable(name, reason) | None
        每次從「online」變「offline」的第一瞬間觸發（不包含重連失敗的後續）。
        每次斷線只發一次通知；不會持續提醒仍在 offline。

    Callbacks 都在 worker thread 被呼叫 — GUI consumer 需自己 marshal 回主執行緒。
    """

    def __init__(
        self,
        account_names: list[str],
        on_state_change: Optional[Callable[[str, str], None]] = None,
        on_connected: Optional[Callable[[str], None]] = None,
        on_disconnected: Optional[Callable[[str, str], None]] = None,
    ):
        self._account_names = list(account_names)
        self._on_state_change = on_state_change
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

        self._stop_flag = False
        self._threads: dict[str, threading.Thread] = {}
        self._last_state: dict[str, str] = {}   # name -> 'init' | 'connecting' | 'online' | 'offline'

    # ── Public API ─────────────────────────────────────────

    def start(self) -> None:
        """啟動所有監測 thread"""
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

        - 設 stop flag，loop 內會在下一次 `ib.sleep(5)` 迴圈檢查後退出
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

    # ── Worker thread：單一帳戶的持久監測迴圈 ───────────────

    def _monitor_loop(self, name: str) -> None:
        """背景 thread：維持與指定帳戶 Gateway 的持久 IB 連線 + 斷線自動重連"""
        asyncio.set_event_loop(asyncio.new_event_loop())

        attempt = 0

        while not self._stop_flag:
            ib = IB()
            try:
                # 每次重連都重建 ctx，萬一 accounts.json 被改也會自動套用新值
                ctx = AccountCtx.from_name(name, ROLE_MONITOR)

                # 標記 connecting
                self._set_state(name, "connecting")

                ib.connect(ctx.host, ctx.port, clientId=ctx.client_id,
                           timeout=CONNECT_TIMEOUT_SEC)
                attempt = 0  # 重連計數歸零

                # 通知 connected（區分第一次 vs 重連由 _on_connected_event 處理）
                self._on_connected_event(name)

                # 連線維持迴圈：ib.sleep 會處理 event loop，斷線時 isConnected 會變 False
                while ib.isConnected() and not self._stop_flag:
                    ib.sleep(POLL_INTERVAL_SEC)

                # 跳出迴圈：不是 stop 就是斷線
                if not self._stop_flag:
                    self._on_disconnected_event(name, "connection lost")
            except Exception as e:
                err = str(e) or type(e).__name__
                self._on_disconnected_event(name, err)
            finally:
                # Always call ib.disconnect() — even when ib.isConnected() is
                # False (e.g. TimeoutError during connect). ib_insync keeps
                # the underlying TCP socket half-open in that case, and
                # leaving it unclosed causes the server to accumulate
                # CLOSE_WAIT sockets. Gateway 10.41 stalls its accept thread
                # after ~2-3 leaked sockets, bricking API handshakes for the
                # whole session (see 2026-04-15 21:25 incident log).
                #
                # ib.disconnect() is idempotent — safe to call on a never-
                # connected or already-disconnected IB instance.
                try:
                    ib.disconnect()
                except Exception:
                    pass

            if self._stop_flag:
                break

            # Backoff 後重試
            delay = BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]
            attempt += 1
            # 分段 sleep 以便快速響應 stop
            for _ in range(delay):
                if self._stop_flag:
                    break
                time.sleep(1)

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
        """連線成功時：更新狀態 + 觸發 on_connected（非 init）"""
        prev = self._last_state.get(name, "init")
        self._set_state(name, "online")
        # 只在「從斷線恢復」時才 fire on_connected（避免 init 時發 reconnect 通知）
        if prev == "offline" and self._on_connected:
            try:
                self._on_connected(name)
            except Exception as e:
                log.warning(f"[GatewayMonitor] on_connected({name}) raised: {e}")

    def _on_disconnected_event(self, name: str, reason: str) -> None:
        """斷線時：更新狀態，只在第一次從 online → offline 時發一次通知"""
        prev = self._last_state.get(name, "init")
        self._set_state(name, "offline")
        # 只在「從連線變斷線」的第一瞬間觸發通知（避免重連 flapping 重複發）
        if prev != "offline" and self._on_disconnected:
            try:
                self._on_disconnected(name, reason)
            except Exception as e:
                log.warning(f"[GatewayMonitor] on_disconnected({name}) raised: {e}")
