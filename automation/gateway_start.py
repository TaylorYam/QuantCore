"""Gateway 啟動（Issue #121）— Panel 一鍵開三個實盤 Gateway 並填入帳密。

用詞見 CONTEXT.md「Gateway 啟動」：開三個實盤 Gateway（Taylor／Rosa／Yvonne），
把各自帳密填進登入視窗後停住；按「登入」和手機 IB Key 由使用者處理。
三個之中只要有一個還在執行（含只停在登入畫面）就整批拒絕。paper Gateway
不在範圍內。

唯一入口 start_live_gateways(ports)；碰外部的地方全部經 ports 注入：
正在執行的 Gateway、認證管理員、Panel 帳密輸入框、開啟 Gateway、填登入視窗。
真的實作在 gateway_start_win.py，測試換成假的。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

LIVE_ACCOUNTS = ("Taylor", "Rosa", "Yvonne")


class LaunchFailed(Exception):
    """Gateway 開不起來（安裝資料夾或 ibgateway.exe 不見了）。"""


class WindowNotFound(Exception):
    """登入視窗在時限內沒出現。"""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class Refused:
    """有實盤 Gateway 還在執行：什麼都沒做。"""
    running: list[str]


@dataclass(frozen=True)
class Cancelled:
    """使用者取消了某個帳戶的帳密輸入框：什麼都沒開。"""
    account: str


@dataclass(frozen=True)
class Started:
    """三個都開了。outcomes: 帳戶 → 結果；details: 帳戶 → 失敗原因（只有失敗的有）。"""
    outcomes: dict[str, str]
    details: dict[str, str] = field(default_factory=dict)


class Ports(Protocol):
    def running_live_accounts(self) -> set[str]: ...
    def get_credentials(self, account: str) -> Credentials | None: ...
    def set_credentials(self, account: str, creds: Credentials) -> None: ...
    def ask_credentials(self, account: str) -> Credentials | None: ...
    def launch(self, account: str) -> None: ...
    def fill_login(self, account: str, creds: Credentials) -> None: ...


def start_live_gateways(ports: Ports):
    running = ports.running_live_accounts()
    if running:
        return Refused(running=[a for a in LIVE_ACCOUNTS if a in running])

    creds = {}
    for account in LIVE_ACCOUNTS:
        found = ports.get_credentials(account)
        if found is None:
            found = ports.ask_credentials(account)
            if found is None:
                return Cancelled(account=account)
            ports.set_credentials(account, found)
        creds[account] = found
    outcomes, details = {}, {}
    for account in LIVE_ACCOUNTS:
        try:
            ports.launch(account)
        except LaunchFailed as e:
            outcomes[account], details[account] = "launch_failed", str(e)

    for account in LIVE_ACCOUNTS:
        if account in outcomes:
            continue
        try:
            ports.fill_login(account, creds[account])
        except WindowNotFound as e:
            outcomes[account], details[account] = "window_not_found", str(e)
        except Exception as e:  # 一個帳戶填失敗不能擋住另外兩個
            outcomes[account], details[account] = "fill_failed", f"{type(e).__name__}: {e}"
        else:
            outcomes[account] = "filled"
    return Started(outcomes={a: outcomes[a] for a in LIVE_ACCOUNTS}, details=details)


def describe(result) -> str:
    """給 Panel 跳窗看的白話結果；只提帳戶名，不含任何帳密。"""
    if isinstance(result, Refused):
        return f"{'、'.join(result.running)} 仍在執行，請先關閉全部實盤 Gateway 再啟動。"
    if isinstance(result, Cancelled):
        return f"已取消（{result.account} 帳密未輸入），沒有開啟任何 Gateway。"
    lines = []
    for account, outcome in result.outcomes.items():
        if outcome == "filled":
            lines.append(f"{account}：已填好帳密")
        else:
            lines.append(f"{account}：未填入 — {result.details.get(account, outcome)}")
    hint = "請確認每個視窗的帳號，按「登入」後到手機確認 IB Key。"
    if any(o != "filled" for o in result.outcomes.values()):
        hint += "未填入的請手動輸入。"
    return "\n".join(lines) + "\n\n" + hint
