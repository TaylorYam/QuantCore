"""新 Panel 正式模式的值班（#154）：Qt 計時器驅動既有的 MonitorEngine。

Rosa poll／TY check／Morning check 的排程與派工全在 `monitor_engine`（與 Tk Panel
同一份）；這裡只是 Qt 的 adapter，對應 Tk 的 `_build_monitor_engine`：
- 計時：單次 QTimer（Tk 的 after）；tick 在 Qt 主執行緒跑，worker 丟 daemon thread。
- 心跳狀態與重連：主視窗的 Gateway 來源（正式模式 = ROLE_MONITOR 的 GatewayMonitor）。
- Telegram：config.json 的共用設定（系統層警報，不看帳戶覆寫，同 Tk `_tg_notify`）。
- Rosa 斷線 10 分鐘後的一次性 recovery，同 Tk 的 on_disconnected。

並行模式不建這個物件（ADR-0012：並存期間值班只由 Tk Panel 跑）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from PySide6.QtCore import QObject, QTimer

ROSA_RECOVERY_DELAY_MS = 10 * 60 * 1000
TY_CHECK_ACCOUNTS = ("Taylor", "Yvonne")


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="qt-panel-watch", daemon=True).start()


def send_system_telegram(msg: str) -> None:
    """值班警報：config.json 的共用 Telegram 設定（同 Tk `_tg_notify`）。"""
    from account_utils import load_base_config
    from notify import tg_send

    try:
        cfg = load_base_config()
    except Exception:
        return
    token, chat_id = cfg.get("telegram_token", ""), cfg.get("telegram_chat_id", "")
    if token and chat_id:
        tg_send(msg, token=token, chat_id=chat_id)


def cold_connect(name: str) -> None:
    """TY check 的冷連線探測：連上就斷，失敗 raise（同 Tk `_cold_connect`）。"""
    from ib_insync import IB
    from account_ctx import AccountCtx
    from account_utils import ROLE_MONITOR

    ctx = AccountCtx.from_name(name, ROLE_MONITOR)
    ib = IB()
    try:
        ib.connect(ctx.host, ctx.port, clientId=ctx.client_id, timeout=10)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


@dataclass
class WatchPorts:
    """值班對外的 I/O（測試換成假的）。"""
    telegram: Callable[[str], None] = send_system_telegram
    cold_connect: Callable[[str], None] = cold_connect
    sleep: Callable[[float], None] = time.sleep
    spawn: Callable[[Callable[[], None]], None] = field(default=_spawn)
    recovery_delay_ms: int = ROSA_RECOVERY_DELAY_MS


class QtWatch(QObject):
    """值班：start() 排好三個監控，stop() 全部取消。計時器在 Qt 主執行緒。"""

    def __init__(self, *, source, account_names: list[str], ports: WatchPorts,
                 log: Callable[[str], None], refresh: Callable[[], None],
                 now: Callable[[], datetime], parent: QObject | None = None):
        super().__init__(parent)
        import monitor_engine as me

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

    def start(self) -> None:
        import monitor_engine as me

        self._running = True
        self.engine.start(me.rosa_poll_spec(self.env))
        self.engine.start(me.ty_check_spec(self.env, TY_CHECK_ACCOUNTS))
        self.engine.start(me.morning_check_spec(self.env))

    def stop(self) -> None:
        self._running = False
        self.engine.stop_all()

    def rosa_went_offline(self) -> None:
        """Rosa 心跳斷了：10 分鐘後試一次重連（窗外也跑，涵蓋 06:00 Gateway 自動重啟）。"""
        import monitor_engine as me

        if self._running:
            self.engine.schedule_once(
                self._ports.recovery_delay_ms,
                me.rosa_recovery_worker(self.env, self._ports.spawn))

    def _notify(self, msg: str) -> None:
        if not self._running:
            return                           # Panel 已關：結果丟掉，同 Tk
        try:
            self._ports.telegram(msg)
        except Exception as e:
            self._log(f"[!] 值班 Telegram 發送失敗：{e}")

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
