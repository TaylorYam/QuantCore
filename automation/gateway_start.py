"""Gateway 啟動（Issue #121、#127）— Panel 一鍵開三個實盤 Gateway、填帳密、依序登入。

用詞見 CONTEXT.md「Gateway 啟動」：開三個實盤 Gateway（Taylor／Rosa／Yvonne），
把各自帳密填進登入視窗，再一次一個帳戶按「登入」並等它登入完成（最多
LOGIN_WAIT_SEC 秒），手機一次只跳一則 IB Key。失敗不重試（連續輸錯會鎖帳號）。
三個之中只要有一個還在執行（含只停在登入畫面）就整批拒絕。paper Gateway
不在範圍內。

唯一入口 start_live_gateways(ports)；碰外部的地方全部經 ports 注入：
正在執行的 Gateway、認證管理員、Panel 帳密輸入框、開啟 Gateway、填登入視窗、
送出登入、等登入完成。
真的實作在 gateway_start_win.py，測試換成假的。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

LIVE_ACCOUNTS = ("Taylor", "Rosa", "Yvonne")

# 送出登入後，等該帳戶登入完成（含手機 IB Key 確認）的上限；逾時就換下一個帳戶（#127）
LOGIN_WAIT_SEC = 30


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
    def running_live_accounts(self) -> set[str]:
        """在跑的實盤帳戶名；認不出是誰的 Gateway 以一段描述代表（也會擋下）。"""
        ...
    def get_credentials(self, account: str) -> Credentials | None: ...
    def set_credentials(self, account: str, creds: Credentials) -> None: ...
    def ask_credentials(self, account: str) -> Credentials | None: ...
    def launch(self, account: str) -> None: ...
    def fill_login(self, account: str, creds: Credentials) -> None: ...
    def submit_login(self, account: str) -> None: ...
    def wait_logged_in(self, account: str, timeout_sec: float) -> bool: ...


def start_live_gateways(ports: Ports):
    running = ports.running_live_accounts()
    if running:
        # 實盤帳戶照固定順序在前；認不出是誰的 Gateway 也算在跑，列在後面
        return Refused(running=[a for a in LIVE_ACCOUNTS if a in running]
                       + sorted(running - set(LIVE_ACCOUNTS)))

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
        except Exception as e:  # 一個帳戶開不起來不能擋住另外兩個
            outcomes[account], details[account] = "launch_failed", f"{type(e).__name__}: {e}"

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

    # 一次一個帳戶：手機一次只跳一則 IB Key，使用者知道在確認哪個帳戶
    for account in LIVE_ACCOUNTS:
        if outcomes[account] != "filled":
            continue
        try:
            ports.submit_login(account)
        except Exception as e:  # 送不出去就停在那個視窗，不重試（連續輸錯會鎖帳號）
            outcomes[account], details[account] = "submit_failed", f"{type(e).__name__}: {e}"
            continue
        if ports.wait_logged_in(account, LOGIN_WAIT_SEC):
            outcomes[account] = "logged_in"
        else:
            outcomes[account] = "login_timeout"
            details[account] = f"已送出，{LOGIN_WAIT_SEC} 秒內沒登入完成"
    return Started(outcomes={a: outcomes[a] for a in LIVE_ACCOUNTS}, details=details)


def describe(result) -> str:
    """給 Panel 跳窗看的白話結果；只提帳戶名，不含任何帳密。"""
    if isinstance(result, Refused):
        return f"{'、'.join(result.running)} 仍在執行，請先關閉全部實盤 Gateway 再啟動。"
    if isinstance(result, Cancelled):
        return f"已取消（{result.account} 帳密未輸入），沒有開啟任何 Gateway。"
    lines = []
    for account, outcome in result.outcomes.items():
        if outcome == "logged_in":
            lines.append(f"{account}：已登入")
        else:
            lines.append(f"{account}：未登入 — {result.details.get(account, outcome)}")
    if all(o == "logged_in" for o in result.outcomes.values()):
        hint = "三個 Gateway 都已登入。"
    else:
        hint = "未登入的請到該 Gateway 視窗手動登入；逾時的可能還在等手機 IB Key 確認。"
    return "\n".join(lines) + "\n\n" + hint
