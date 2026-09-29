"""Gateway 連線狀態來源（#147）。

主視窗只認 `GatewaySource`：start(on_state) 之後，每次狀態變化呼叫
on_state(name, state, detail)，state ∈ init／connecting／online／offline。
回呼可能來自背景 thread，主視窗負責轉回 Qt 主執行緒。recheck() 對應 Tk 的
Check Now：心跳斷了不會自己重連（2026-04-15 事故，見 gateway_monitor），要
重新偵測就得有人按。
"""
from __future__ import annotations

from typing import Callable, Protocol

from account_utils import ROLE_PANEL_READONLY
from gateway_monitor import GatewayMonitor

from qt_panel.mode import PanelMode

OnState = Callable[[str, str, str], None]


class GatewaySource(Protocol):
    def start(self, on_state: OnState) -> None: ...
    def stop(self) -> None: ...
    def recheck(self) -> None: ...


class MonitorGatewaySource:
    """包 GatewayMonitor：每帳戶一條長連線心跳，斷了就回報 offline（不重連）。"""

    def __init__(self, account_names: list[str], *, role: int, readonly: bool,
                 monitor_cls=GatewayMonitor):
        self._names = list(account_names)
        self._role = role
        self._readonly = readonly
        self._monitor_cls = monitor_cls
        self._monitor = None

    def start(self, on_state: OnState) -> None:
        self._monitor = self._monitor_cls(
            self._names,
            on_state_change=lambda name, state: on_state(name, state, ""),
            on_disconnected=lambda name, reason: on_state(name, "offline", reason),
            role=self._role,
            readonly=self._readonly,
        )
        self._monitor.start()

    def stop(self) -> None:
        if self._monitor is not None:
            self._monitor.stop()

    def recheck(self) -> None:
        """已斷的帳戶各重連一次；還連著或正在連的不動（同 Tk Check Now）。"""
        if self._monitor is None:
            return
        for name in self._names:
            self._monitor.restart_account_thread(name)


def gateway_source_for(mode: PanelMode, account_names: list[str], *,
                       monitor_cls=GatewayMonitor) -> MonitorGatewaySource:
    """並行模式：唯讀身分（ROLE_PANEL_READONLY）＋唯讀連線，不和 Tk Panel 搶 clientId。"""
    if mode is PanelMode.PARALLEL:
        return MonitorGatewaySource(account_names, role=ROLE_PANEL_READONLY,
                                    readonly=True, monitor_cls=monitor_cls)
    raise ValueError(f"unsupported panel mode: {mode}")
