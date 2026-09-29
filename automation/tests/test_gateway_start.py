"""Gateway 啟動（#121）：一個入口 start_live_gateways，四個 I/O 換成假的。

不開真的 Gateway、不碰認證管理員、不打字；只看回傳結果和假 port 被要求做了什麼。
"""
from __future__ import annotations

import pytest

from gateway_start import (
    Cancelled, Credentials, LaunchFailed, Refused, Started, WindowNotFound,
    describe, start_live_gateways,
)

CREDS = {
    "Taylor": Credentials("taylor_user", "taylor-secret"),
    "Rosa": Credentials("rosa_user", "rosa-secret"),
    "Yvonne": Credentials("yvonne_user", "yvonne-secret"),
}


class FakePorts:
    """In-memory stand-ins for the four ports; records what they were asked to do."""

    def __init__(self, running=(), stored=None, answers=None,
                 no_window=(), launch_fails=(), fill_errors=None, launch_errors=None,
                 never_logs_in=(), submit_errors=None):
        self.running = set(running)
        self.stored = dict(CREDS if stored is None else stored)
        self.answers = dict(answers or {})
        self.no_window = set(no_window)
        self.launch_fails = set(launch_fails)
        self.fill_errors = dict(fill_errors or {})
        self.launch_errors = dict(launch_errors or {})
        self.never_logs_in = set(never_logs_in)
        self.submit_errors = dict(submit_errors or {})
        self.events: list[tuple[str, str]] = []
        self.launched: list[str] = []
        self.typed: list[tuple[str, Credentials]] = []
        self.asked: list[str] = []
        self.credentials_read = False

    # Running Gateways
    def running_live_accounts(self) -> set[str]:
        return set(self.running)

    # Credential store
    def get_credentials(self, account):
        self.credentials_read = True
        return self.stored.get(account)

    def set_credentials(self, account, creds):
        self.stored[account] = creds

    # Operator prompt (Panel dialog)
    def ask_credentials(self, account):
        self.asked.append(account)
        return self.answers.get(account)

    # Launcher
    def launch(self, account):
        if account in self.launch_fails:
            raise LaunchFailed(f"找不到 {account} 的 ibgateway.exe")
        if account in self.launch_errors:
            raise self.launch_errors[account]
        self.launched.append(account)

    # Login filler
    def fill_login(self, account, creds):
        if account in self.no_window:
            raise WindowNotFound(f"{account} 登入視窗沒出現")
        if account in self.fill_errors:
            raise self.fill_errors[account]
        self.typed.append((account, creds))
        self.events.append(("fill", account))

    # Login submitter
    def submit_login(self, account):
        if account in self.submit_errors:
            raise self.submit_errors[account]
        self.events.append(("submit", account))

    def wait_logged_in(self, account, timeout_sec):
        self.events.append(("wait", account))
        self.wait_timeouts = getattr(self, "wait_timeouts", []) + [timeout_sec]
        return account not in self.never_logs_in


@pytest.mark.parametrize("running", ["Taylor", "Rosa", "Yvonne"])
def test_any_live_gateway_running_refuses_and_starts_nothing(running):
    ports = FakePorts(running={running})

    result = start_live_gateways(ports)

    assert result == Refused(running=[running])
    assert ports.launched == []
    assert ports.typed == []
    assert not ports.credentials_read


def test_refusal_names_every_running_live_account():
    ports = FakePorts(running={"Yvonne", "Rosa"})

    assert start_live_gateways(ports) == Refused(running=["Rosa", "Yvonne"])


def test_nothing_running_launches_all_three_and_fills_each_with_its_own_credentials():
    ports = FakePorts()

    result = start_live_gateways(ports)

    assert result == Started(outcomes={
        "Taylor": "logged_in", "Rosa": "logged_in", "Yvonne": "logged_in",
    })
    assert ports.launched == ["Taylor", "Rosa", "Yvonne"]
    assert ports.typed == [
        ("Taylor", Credentials("taylor_user", "taylor-secret")),
        ("Rosa", Credentials("rosa_user", "rosa-secret")),
        ("Yvonne", Credentials("yvonne_user", "yvonne-secret")),
    ]


def test_missing_credentials_are_asked_for_stored_and_used():
    rosa = Credentials("rosa_new", "rosa-new-secret")
    stored = {k: v for k, v in CREDS.items() if k != "Rosa"}
    ports = FakePorts(stored=stored, answers={"Rosa": rosa})

    result = start_live_gateways(ports)

    assert ports.asked == ["Rosa"]
    assert ports.stored["Rosa"] == rosa
    assert ("Rosa", rosa) in ports.typed
    assert result.outcomes["Rosa"] == "logged_in"


def test_cancelling_the_credential_prompt_starts_nothing():
    stored = {k: v for k, v in CREDS.items() if k != "Yvonne"}
    ports = FakePorts(stored=stored, answers={})  # operator cancels → None

    result = start_live_gateways(ports)

    assert result == Cancelled(account="Yvonne")
    assert ports.launched == []
    assert "Yvonne" not in ports.stored


def test_one_login_window_never_appearing_does_not_stop_the_others():
    ports = FakePorts(no_window={"Taylor"})

    result = start_live_gateways(ports)

    assert result.outcomes == {
        "Taylor": "window_not_found", "Rosa": "logged_in", "Yvonne": "logged_in",
    }
    assert result.details["Taylor"] == "Taylor 登入視窗沒出現"
    assert [a for a, _ in ports.typed] == ["Rosa", "Yvonne"]


def test_one_gateway_failing_to_launch_is_reported_and_not_filled():
    ports = FakePorts(launch_fails={"Rosa"})

    result = start_live_gateways(ports)

    assert result.outcomes == {
        "Taylor": "logged_in", "Rosa": "launch_failed", "Yvonne": "logged_in",
    }
    assert result.details["Rosa"] == "找不到 Rosa 的 ibgateway.exe"
    assert [a for a, _ in ports.typed] == ["Taylor", "Yvonne"]


def test_an_error_while_typing_is_reported_as_fill_failed():
    ports = FakePorts(fill_errors={"Yvonne": OSError("SetForegroundWindow 被拒")})

    result = start_live_gateways(ports)

    assert result.outcomes["Yvonne"] == "fill_failed"
    assert "SetForegroundWindow 被拒" in result.details["Yvonne"]
    assert result.outcomes["Taylor"] == result.outcomes["Rosa"] == "logged_in"


def test_results_never_show_a_password():
    ports = FakePorts(no_window={"Taylor"}, launch_fails={"Rosa"},
                      fill_errors={"Yvonne": OSError("boom")})

    shown = repr(start_live_gateways(ports)) + repr(CREDS)

    for creds in CREDS.values():
        assert creds.password not in shown


def test_refusal_message_tells_the_operator_what_to_close():
    assert describe(Refused(running=["Rosa", "Yvonne"])) == (
        "Rosa、Yvonne 仍在執行，請先關閉全部實盤 Gateway 再啟動。"
    )


def test_cancel_message_says_nothing_was_started():
    assert describe(Cancelled(account="Yvonne")) == "已取消（Yvonne 帳密未輸入），沒有開啟任何 Gateway。"


def test_started_message_lists_each_account_and_what_to_do_next():
    result = Started(
        outcomes={"Taylor": "logged_in", "Rosa": "window_not_found", "Yvonne": "login_timeout"},
        details={"Rosa": "Rosa 登入視窗 120 秒內沒出現", "Yvonne": "已送出，30 秒內沒登入完成"},
    )

    assert describe(result) == (
        "Taylor：已登入\n"
        "Rosa：未登入 — Rosa 登入視窗 120 秒內沒出現\n"
        "Yvonne：未登入 — 已送出，30 秒內沒登入完成\n"
        "\n"
        "未登入的請到該 Gateway 視窗手動登入；逾時的可能還在等手機 IB Key 確認。"
    )


def test_all_logged_in_message_needs_no_action():
    result = Started(outcomes={"Taylor": "logged_in", "Rosa": "logged_in", "Yvonne": "logged_in"})

    assert describe(result) == (
        "Taylor：已登入\nRosa：已登入\nYvonne：已登入\n\n三個 Gateway 都已登入。"
    )


def test_an_unidentifiable_gateway_also_refuses_and_is_named():
    ports = FakePorts(running={"無法辨識的 Gateway（pid 4242）", "Rosa"})

    assert start_live_gateways(ports) == Refused(
        running=["Rosa", "無法辨識的 Gateway（pid 4242）"])
    assert ports.launched == []


def test_an_unexpected_launch_error_only_fails_that_account():
    ports = FakePorts(launch_errors={"Taylor": PermissionError("ibgateway1.exe 改名被拒")})

    result = start_live_gateways(ports)

    assert result.outcomes == {
        "Taylor": "launch_failed", "Rosa": "logged_in", "Yvonne": "logged_in",
    }
    assert "ibgateway1.exe 改名被拒" in result.details["Taylor"]


def test_logins_are_submitted_one_account_at_a_time_after_all_are_filled():
    ports = FakePorts()

    result = start_live_gateways(ports)

    assert ports.events == [
        ("fill", "Taylor"), ("fill", "Rosa"), ("fill", "Yvonne"),
        ("submit", "Taylor"), ("wait", "Taylor"),
        ("submit", "Rosa"), ("wait", "Rosa"),
        ("submit", "Yvonne"), ("wait", "Yvonne"),
    ]
    assert ports.wait_timeouts == [30, 30, 30]
    assert result.outcomes == {"Taylor": "logged_in", "Rosa": "logged_in", "Yvonne": "logged_in"}


def test_a_login_that_does_not_complete_in_time_moves_on_to_the_next_account():
    ports = FakePorts(never_logs_in={"Rosa"})

    result = start_live_gateways(ports)

    assert result.outcomes == {"Taylor": "logged_in", "Rosa": "login_timeout", "Yvonne": "logged_in"}
    assert ("submit", "Yvonne") in ports.events


def test_an_account_whose_window_was_not_filled_is_never_submitted():
    ports = FakePorts(no_window={"Taylor"}, fill_errors={"Yvonne": OSError("boom")})

    start_live_gateways(ports)

    assert [e for e in ports.events if e[0] == "submit"] == [("submit", "Rosa")]


def test_a_login_that_cannot_be_submitted_fails_only_that_account():
    ports = FakePorts(submit_errors={"Taylor": OSError("Taylor 登入視窗被切走了")})

    result = start_live_gateways(ports)

    assert result.outcomes == {"Taylor": "submit_failed", "Rosa": "logged_in", "Yvonne": "logged_in"}
    assert "Taylor 登入視窗被切走了" in result.details["Taylor"]
    assert ("wait", "Taylor") not in ports.events
