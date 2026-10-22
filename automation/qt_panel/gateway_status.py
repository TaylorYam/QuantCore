"""Gateway 連線狀態來源（#147）。

主視窗只認 `GatewaySource`：start(on_state) 之後，每次狀態變化呼叫
on_state(name, state, detail)，state ∈ init／connecting／online／offline。
回呼可能來自背景 thread，主視窗負責轉回 Qt 主執行緒。recheck() 是「重新檢查」：
心跳斷了不會自己重連（2026-04-15 事故，見 gateway_monitor），要重新偵測就得有人
按（或 Morning check）。

值班（#154）另外要問心跳狀態與重連（is_online／restart）。心跳用值班身分
ROLE_MONITOR、一般連線（非唯讀）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from account_utils import ROLE_MONITOR
from gateway_monitor import GatewayMonitor

OnState = Callable[[str, str, str], None]


@dataclass(frozen=True)
class GatewayTransition:
    """一次 Gateway 狀態回報後主視窗要做的事（#231）。"""
    update_tray: bool = False       # 系統匣顏色跟著遠端 Gateway 帳戶
    remote_offline: bool = False    # 告訴值班：遠端 Gateway 剛離線
    log: str = ""                   # 要寫的 log；空字串 = 不寫


def gateway_transition(name: str, previous: str | None, state: str, detail: str,
                       remote_account: str | None) -> GatewayTransition:
    """previous：這個帳戶上一次的狀態（第一次回報是 None）。只有狀態真的變了才換托盤色、
    才寫 log；從 init 第一次連上不寫「已連上」。"""
    changed = state != previous
    remote = name == remote_account and changed
    log = ""
    if previous is not None and changed:
        if state == "offline":
            log = f"[GW] {name} 離線" + (f" — {detail}" if detail else "")
        elif state == "online" and previous != "init":
            log = f"[GW] {name} 已連上"
    return GatewayTransition(update_tray=remote, remote_offline=remote and state == "offline",
                             log=log)


class GatewaySource(Protocol):
    def start(self, on_state: OnState) -> None: ...
    def stop(self) -> None: ...
    def recheck(self) -> None: ...


class MonitorGatewaySource:
    """包 GatewayMonitor：每帳戶一條長連線心跳，斷了就回報 offline（不重連）。"""

    def __init__(self, account_names: list[str], *, monitor_cls=GatewayMonitor):
        self._names = list(account_names)
        self._monitor_cls = monitor_cls
        self._monitor = None
        self._on_state: OnState | None = None

    def start(self, on_state: OnState) -> None:
        self._on_state = on_state
        self._monitor = self._monitor_cls(
            self._names,
            on_state_change=lambda name, state: on_state(name, state, ""),
            on_disconnected=lambda name, reason: on_state(name, "offline", reason),
            role=ROLE_MONITOR,
            readonly=False,
        )
        self._monitor.start()

    def stop(self) -> None:
        if self._monitor is not None:
            self._monitor.stop()

    # ── 值班用 ───────────────────────────────────────────────────────

    def ready(self) -> bool:
        return self._monitor is not None

    def is_online(self, name: str) -> bool:
        return self._monitor is not None and self._monitor.is_online(name)

    def restart(self, name: str) -> bool:
        """重開一個帳戶的心跳；已經連著或正在連的不動（GatewayMonitor 自己判斷）。"""
        return self._monitor is not None and self._monitor.restart_account_thread(name)

    def recheck(self) -> None:
        """已斷的帳戶各重連一次；還連著或正在連的不動。"""
        if self._monitor is None:
            return
        for name in self._names:
            # 重新開始連的帳戶先顯示連線中，登入後的檢查才會等它有結果
            if self._monitor.restart_account_thread(name) and self._on_state is not None:
                self._on_state(name, "connecting", "")

