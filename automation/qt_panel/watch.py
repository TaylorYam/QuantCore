"""Panel 的值班（#154）：Qt 計時器驅動既有的 MonitorEngine。

Rosa poll（遠端 Gateway 帳戶）／TY check／Morning check 的排程與派工全在 `monitor_engine`；這裡只是
Qt 的 adapter：
- 計時：單次 QTimer；tick 在 Qt 主執行緒跑，worker 丟 daemon thread。
- 心跳狀態與重連：主視窗的 Gateway 來源（ROLE_MONITOR 的 GatewayMonitor）。
- Telegram：config.json 的共用設定（系統層警報，不看帳戶覆寫）。
- 遠端 Gateway 帳戶（Rosa）斷線 10 分鐘後的一次性 recovery。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from PySide6.QtCore import QObject, QTimer

from notify import send_system

ROSA_RECOVERY_DELAY_MS = 10 * 60 * 1000
# 心跳開始後等一下再排值班，
# 窗內開啟時第一次 Rosa poll 才不會碰上還在連線中的心跳
WATCH_START_DELAY_MS = 1500


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="qt-panel-watch", daemon=True).start()


def cold_connect(name: str) -> None:
    """TY check 的冷連線探測：經 Broker 連上就斷，失敗 raise。"""
    from account_ctx import AccountCtx
    from broker import ROLE_MONITOR, Broker

    Broker().connect(AccountCtx.from_name(name, ROLE_MONITOR), timeout=10).close()


@dataclass
class WatchPorts:
    """值班對外的 I/O（測試換成假的）。"""
    telegram: Callable[[str], bool] = send_system
    cold_connect: Callable[[str], None] = cold_connect
    sleep: Callable[[float], None] = time.sleep
    spawn: Callable[[Callable[[], None]], None] = field(default=_spawn)
    recovery_delay_ms: int = ROSA_RECOVERY_DELAY_MS
    start_delay_ms: int = WATCH_START_DELAY_MS


class QtWatch(QObject):
    """值班：start() 排好三個監控，stop() 全部取消。計時器在 Qt 主執行緒。"""

    def __init__(self, *, source, account_names: list[str], ports: WatchPorts,
                 log: Callable[[str], None], refresh: Callable[[], None],
                 now: Callable[[], datetime], remote_account: str | None,
                 ty_check_accounts: tuple[str, ...], parent: QObject | None = None):
        """remote_account：遠端 Gateway 帳戶（poll 與斷線 recovery，None = 不跑）；
        ty_check_accounts：週二三 TY check 的帳戶（#211，都來自 accounts.json）。"""
        super().__init__(parent)
        import monitor_engine as me

        self._remote_account = remote_account
        self._ty_check_accounts = tuple(ty_check_accounts)
        self._ports = ports
        self._log = log
        self.env = me.MonitorEnv(
            monitor_ready=source.ready,
            is_online=source.is_online,
            restart=source.restart,
            cold_connect=ports.cold_connect,
            accounts=lambda: list(account_names),
            refresh_ui=refresh,
            log=log,                       # 主視窗的 log 任何執行緒都能叫
            notify=self._notify,
            now=now,
            sleep=ports.sleep,
        )
        self.engine = me.MonitorEngine(self.env, schedule=self._schedule,
                                       cancel=self._cancel, spawn=ports.spawn)
        self._running = False
        self._start_timer: QTimer | None = None

    def start(self) -> None:
        self._running = True
        if self._ports.start_delay_ms > 0:
            self._start_timer = self._schedule(self._ports.start_delay_ms, self._start_specs)
        else:
            self._start_specs()

    def _start_specs(self) -> None:
        import monitor_engine as me

        self._start_timer = None
        if not self._running:
            return
        if self._remote_account:
            self.engine.start(me.rosa_poll_spec(self.env, self._remote_account))
        if self._ty_check_accounts:
            self.engine.start(me.ty_check_spec(self.env, self._ty_check_accounts))
        self.engine.start(me.morning_check_spec(self.env))

    def stop(self) -> None:
        self._running = False
        if self._start_timer is not None:
            self._cancel(self._start_timer)
            self._start_timer = None
        self.engine.stop_all()

    def rosa_went_offline(self) -> None:
        """遠端 Gateway 心跳斷了：10 分鐘後試一次重連（窗外也跑，涵蓋 06:00 Gateway 自動重啟）。"""
        import monitor_engine as me

        if self._running and self._remote_account:
            self.engine.schedule_once(
                self._ports.recovery_delay_ms,
                me.rosa_recovery_worker(self.env, self._ports.spawn, self._remote_account))

    def _notify(self, msg: str) -> None:
        if not self._running:
            return                           # Panel 已關：結果丟掉
        try:
            sent = self._ports.telegram(msg)
        except Exception as e:
            self._log(f"[!] 值班 Telegram 發送失敗：{e}")
            return
        if sent is False:
            self._log("[!] 值班 Telegram 沒送出（共用頻道沒設定或送不出去）")

    def _schedule(self, ms: int, fn: Callable[[], None]) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(fn)
        timer.timeout.connect(timer.deleteLater)
        timer.start(ms)
        return timer

    @staticmethod
    def _cancel(timer: QTimer) -> None:
        try:
            timer.stop()
            timer.deleteLater()
        except RuntimeError:                 # 已觸發並釋放
            pass
