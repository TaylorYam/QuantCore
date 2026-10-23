"""
QuantCore — 值班監控引擎（候選 5：panel 監控下沉）

Rosa poll / TY check / Morning check 三個定時監控的「排程 + 派工」唯一實作。
Panel 是 caller：`qt_panel.watch` 提供 Qt adapter（QTimer 計時、worker→主線程
marshal、系統層 Telegram）；測試提供假時鐘 + 假 env — 接線行為（tick→spawn→re-arm 順序、
取消、一次性排程、無警報不發 TG）從此有考卷。

分工
────
- 「什麼時候該跑」：純函式 `next_*_time(now)`（rosa_poll / ty_check /
  morning_check 模組，各自有完整測試）— 本模組只負責把它們接上計時器。
- 「查完該做什麼」：三張 spec 的 worker（自 control_panel 下沉），透過
  MonitorEnv ports 做 I/O — 不 import Tk、不認得 GatewayMonitor 實體。
- 「畫面怎麼顯示」：留在 panel（env.log / env.refresh_ui 的實作端）。

防洪設計（原樣保留，勿改）：Gateway 斷線事件**不直接發 TG** — Rosa 的警報
只透過 30 分輪詢 gate（rosa_poll_decision），盤外 flapping 才不會洗版。
事件路徑只有「斷線 10 分鐘後排一發 one-shot recovery」（schedule_once）。

執行緒模型（沿 panel 原設計）：tick 在計時器 context（Qt = 主線程）執行
spec.pre 後，把 worker 丟 daemon thread，再立刻排下一 tick；worker 內只准
碰 env ports — marshal 回 UI 線程是 adapter（panel）的責任。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import _repo_root  # noqa: F401  Morning check 讀 strategies 的設定（#232）


# ── Ports ─────────────────────────────────────────────────────────────

@dataclass
class MonitorEnv:
    """監控 worker 需要的全部 I/O 能力（panel / 測試各自實作）。

    log / notify 的實作端負責線程 marshal 與「panel 已銷毀」的例外吞噬 —
    worker 直接呼叫即可。
    """
    monitor_ready: Callable[[], bool]          # GatewayMonitor 是否已啟動
    is_online: Callable[[str], bool]           # 心跳連線目前狀態
    restart: Callable[[str], bool]             # restart_account_thread
    cold_connect: Callable[[str], None]        # 冷連線探測（失敗 raise）
    accounts: Callable[[], list[str]]          # 全帳戶名單（morning check 用）
    refresh_ui: Callable[[], None]             # Check Now 等效（tick context 執行）
    log: Callable[[str], None]                 # Output Log 一行
    notify: Callable[[str], None]              # Telegram 發送
    now: Callable[[], datetime]                # 台北時間
    sleep: Callable[[float], None] = time.sleep


@dataclass
class MonitorSpec:
    """一個定時監控的完整定義。"""
    label: str                                     # log 前綴，如 "Rosa poll"
    next_time: Callable[[datetime], datetime]      # 純排程：now → 下次到期
    workers: Callable[[], list[Callable[[], None]]]  # 本次 tick 要 spawn 的 worker 們
    pre: Optional[Callable[[], None]] = None       # tick context（主線程）前置步
    arm_text: str = "next at"                      # arm log 用詞（Rosa 歷史用 "next tick at"）
    run_at_start: Optional[Callable[[datetime], bool]] = None  # 啟動當下是否立跑一次
    start_log_immediate: str = ""                  # 立跑時的啟動 log
    start_log_waiting: str = ""                    # 不立跑時的啟動 log


# ── Engine ────────────────────────────────────────────────────────────

class MonitorEngine:
    """排程引擎：arm → tick →（pre → spawn workers → re-arm）→ …

    timer ports：
      schedule(delay_ms, fn) -> handle   （Qt: 單次 QTimer）
      cancel(handle)                     （Qt: 停掉該 QTimer）
      spawn(fn)                          （daemon thread）
    """

    def __init__(self, env: MonitorEnv,
                 schedule: Callable[[int, Callable[[], None]], object],
                 cancel: Callable[[object], None],
                 spawn: Callable[[Callable[[], None]], None]):
        self.env = env
        self._schedule = schedule
        self._cancel = cancel
        self._spawn = spawn
        self._handles: dict[str, object] = {}      # label -> pending after handle
        self._once_handles: list[object] = []      # schedule_once pending handles

    # ── 週期監控 ──────────────────────────────────────────────

    def start(self, spec: MonitorSpec) -> None:
        """啟動一個監控：視 run_at_start 決定是否立跑一次，然後 arm。"""
        self._handles[spec.label] = None
        if spec.run_at_start is not None:
            if spec.run_at_start(self.env.now()):
                if spec.start_log_immediate:
                    self.env.log(spec.start_log_immediate)
                for w in spec.workers():
                    self._spawn(w)
            elif spec.start_log_waiting:
                self.env.log(spec.start_log_waiting)
        self._arm(spec)

    def _arm(self, spec: MonitorSpec) -> None:
        now = self.env.now()
        nxt = spec.next_time(now)
        delay_ms = max(1, int((nxt - now).total_seconds() * 1000))
        self._handles[spec.label] = self._schedule(
            delay_ms, lambda: self._tick(spec))
        self.env.log(f"[GW] {spec.label}: {spec.arm_text} "
                     f"{nxt.strftime('%Y-%m-%d %H:%M %Z')}")

    def _tick(self, spec: MonitorSpec) -> None:
        """計時器 callback（Qt adapter = 主線程）：前置步 → 派工 → 排下一次。"""
        if spec.pre is not None:
            spec.pre()
        for w in spec.workers():
            self._spawn(w)
        self._arm(spec)

    def stop_all(self) -> None:
        """取消所有 pending 排程（panel 關閉用）；cancel 失敗靜默。"""
        for label, handle in list(self._handles.items()):
            if handle is not None:
                try:
                    self._cancel(handle)
                except Exception:
                    pass
            self._handles[label] = None
        for handle in self._once_handles:
            try:
                self._cancel(handle)
            except Exception:
                pass
        self._once_handles.clear()

    # ── 一次性排程（事件驅動，如 Rosa 斷線 10 分鐘後 recovery）──

    def schedule_once(self, delay_ms: int, fn: Callable[[], None]) -> None:
        self._once_handles.append(self._schedule(delay_ms, fn))


# ── Spec factories（worker 派工邏輯，自 control_panel 下沉）───────────

def remote_poll_spec(env: MonitorEnv, account: str) -> MonitorSpec:
    """遠端 Gateway 帳戶（accounts.json 的 remote_gateway，現狀 = Rosa，#211）的
    30 分鐘市場窗輪詢（台北 21:00–05:00，XX:00 / XX:30 對齊）。log 前綴由帳戶名組成。"""
    from market_calendar import trading_session_date_for
    from rosa_poll import next_rosa_poll_time, rosa_poll_decision

    def run() -> None:
        if not env.monitor_ready():
            return  # GatewayMonitor not yet started — skip this cycle
        now = env.now()
        msg = rosa_poll_decision(
            now_taipei=now,
            is_online=lambda: env.is_online(account),
            restart=lambda: env.restart(account),
            sleep=env.sleep,
            account=account,
        )
        ts = now.strftime("%H:%M")
        if msg is None:
            env.log(f"[GW] {account} poll {ts} → ok")
        else:
            env.log(f"[GW] {account} poll {ts} → ALERT")
            env.notify(msg)

    return MonitorSpec(
        label=f"{account} poll",
        next_time=next_rosa_poll_time,
        workers=lambda: [run],
        arm_text="next tick at",
        run_at_start=lambda now: trading_session_date_for(now) is not None,
        start_log_immediate=f"[GW] {account} poll: panel start inside window → immediate tick",
        start_log_waiting=f"[GW] {account} poll: panel start outside window → scheduling",
    )


def remote_recovery_worker(env: MonitorEnv,
                           spawn: Callable[[Callable[[], None]], None],
                           account: str) -> Callable[[], None]:
    """遠端 Gateway 帳戶（現狀 Rosa）斷線 10 分鐘後的一次性 recovery（窗外也會跑，涵蓋 06:00 台北
    Gateway 自動重啟情境）。已回線則跳過 — restart_account_thread 對活線程
    也會拒絕 spawn，此處是雙保險。restart 丟 daemon thread 以免未來變種
    阻塞計時器線程。"""
    def run() -> None:
        if not env.monitor_ready():
            return
        if env.is_online(account):
            env.log(f"[GW] {account} one-shot recovery: already online, skip")
            return
        env.log(f"[GW] {account} one-shot recovery: attempting reconnect")
        spawn(lambda: env.restart(account))
    return run


def ty_check_spec(env: MonitorEnv, accounts: tuple[str, ...]) -> MonitorSpec:
    """週二・週三 20:05 開盤前驗證（accounts.json ty_check 的帳戶，現狀 = Taylor /
    Yvonne，#211；每帳戶一條 worker）。

    Hybrid：心跳連線已 ONLINE = 活體證明，跳過冷連線（避免 clientId 撞號）；
    否則冷連線探測（10s timeout）。成功只寫 Output Log，失敗才發 TG（#26）。"""
    from ty_check import format_ty_message, next_ty_check_time, verify_one_account

    def make_worker(name: str) -> Callable[[], None]:
        def run() -> None:
            if env.monitor_ready() and env.is_online(name):
                msg = format_ty_message(name, success=True)
            else:
                msg = verify_one_account(name, lambda: env.cold_connect(name))
            ok = '✅' in msg
            env.log(f"[GW] TY check {name} → {'ok' if ok else 'ALERT'}")
            if not ok:
                env.notify(msg)
        return run

    return MonitorSpec(
        label="TY check",
        next_time=next_ty_check_time,
        workers=lambda: [make_worker(n) for n in accounts],
    )


def morning_check_spec(env: MonitorEnv) -> MonitorSpec:
    """每 NYSE 交易日 07:00 台北：主線程先跑 Check Now 等效 re-probe（pre），
    worker 等 12 秒沉澱後只在有帳戶離線時發 TG（全綠靜默）；另附
    constituents staleness 提醒（獨立於 gateway 狀態）。"""
    from morning_check import (MORNING_SETTLE_SEC,
                               constituents_staleness_warning,
                               format_morning_check_message,
                               next_morning_check_time,
                               read_constituents_last_date)

    def pre() -> None:
        env.log("[GW] Morning check 07:00 → re-probing all Gateways")
        env.refresh_ui()   # identical to the Check Now button

    def run() -> None:
        if not env.monitor_ready():
            return  # GatewayMonitor not yet started — skip this cycle
        env.sleep(MORNING_SETTLE_SEC)

        offline = [name for name in env.accounts() if not env.is_online(name)]
        msg = format_morning_check_message(offline)
        if msg is None:
            env.log("[GW] Morning check → all online")
        else:
            env.log(f"[GW] Morning check → ALERT "
                    f"({len(offline)} offline: {', '.join(offline)})")
            env.notify(msg)

        # ADR-0002 constituents staleness reminder — independent of gateway
        # state (a stale snapshot matters precisely on days everything else
        # is healthy). Reminder only; refresh 自動化見 ADR-0009。
        from strategies.sp500_momentum import config as sp_cfg
        warn = constituents_staleness_warning(
            read_constituents_last_date(sp_cfg.REF_DIR / sp_cfg.CONST_FILE),
            env.now().date(),
        )
        if warn is not None:
            env.log("[GW] Morning check → constituents stale")
            env.notify(warn)

    return MonitorSpec(
        label="Morning check",
        next_time=next_morning_check_time,
        workers=lambda: [run],
        pre=pre,
    )
