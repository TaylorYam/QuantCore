"""Trading 各畫面共用：背景執行緒跑 IB，結果排回 Qt 主執行緒。"""
from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot


class MainThread(QObject):
    """背景執行緒把 callback 排回主執行緒執行。"""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run, Qt.QueuedConnection)

    @Slot(object)
    def _run(self, fn) -> None:
        fn()


def background(main: MainThread, work: Callable[[], object],
               done: Callable[[object, str | None], None], *, name: str) -> None:
    """在背景跑 work()；結束後在主執行緒呼叫 done(結果, 錯誤訊息或 None)。"""
    def run():
        try:
            result, error = work(), None
        except Exception as exc:
            result, error = None, str(exc) or type(exc).__name__
        main.call.emit(lambda: done(result, error))
    threading.Thread(target=run, name=name, daemon=True).start()


class QtRunner:
    """trading_flow 的 runner（Qt 版）：背景執行緒跑 work，結果與 post 都排回主執行緒。"""

    def __init__(self, main: MainThread, name: str):
        self._main = main
        self._name = name

    def run(self, work: Callable[[], object],
            done: Callable[[object, str | None], None]) -> None:
        background(self._main, work, done, name=self._name)

    def post(self, fn: Callable[[], None]) -> None:
        self._main.call.emit(fn)
