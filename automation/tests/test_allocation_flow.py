"""「調整…」Allocation 的流程（qt_panel.allocation_flow，#231）與 Gateway 狀態轉換規則
（gateway_status.gateway_transition）：純規則，不開 Qt。

Panel 上同樣的情境另有 Qt 接線題（test_qt_panel 的 Allocation 與 Gateway 測試）。
No IB, no files.
"""
from __future__ import annotations

import pytest

from qt_panel.allocation_flow import edit_allocation
from qt_panel.gateway_status import GatewayTransition, gateway_transition


class Dialogs:
    def __init__(self, answers=(), confirms=()):
        self.answers, self.confirm_answers = list(answers), list(confirms)
        self.asked, self.confirms, self.messages = [], [], []

    def ask_allocation(self, account, current):
        self.asked.append((account, tuple(current)))
        return self.answers.pop(0) if self.answers else None

    def confirm(self, title, text, *, warning=False):
        self.confirms.append((title, text, warning))
        return self.confirm_answers.pop(0) if self.confirm_answers else True

    def show_message(self, title, text, *, error=False):
        self.messages.append((title, text, error))


class Config:
    """accounts.json 的假讀寫：read 記下每次重讀，save 改掉讀取值。"""

    def __init__(self, cfg=None, read_error=None, save_error=None):
        self.cfg = dict(cfg or {"alloc_sr": 0.80, "alloc_sp500": 0.50, "alloc_skewls": 0.0})
        self.read_error, self.save_error = read_error, save_error
        self.reads, self.saves = 0, []

    def read(self, name):
        self.reads += 1
        if self.read_error:
            raise self.read_error
        return dict(self.cfg)

    def save(self, name, updates):
        if self.save_error:
            raise self.save_error
        self.saves.append((name, dict(updates)))
        self.cfg.update(updates)


def run(config, dialogs, name="Taylor"):
    logs = []
    edit_allocation(name, read=config.read, save=config.save, dialogs=dialogs, log=logs.append)
    return logs


def test_saves_only_the_alloc_fields_then_reads_again():
    config = Config({"alloc_sr": 0.75, "alloc_sp500": 0.45, "alloc_skewls": 0.30})
    dialogs = Dialogs(answers=[(0.70, 0.40, 0.30)])
    logs = run(config, dialogs, "Yvonne")
    assert dialogs.asked == [("Yvonne", (0.75, 0.45, 0.30))]
    assert config.saves == [("Yvonne", {"alloc_sr": 0.70, "alloc_sp500": 0.40,
                                        "alloc_skewls": 0.30})]
    (title, text, warning), = dialogs.confirms
    assert "Yvonne" in text and "140% (1.4x)" in text and not warning
    assert config.reads == 2                                      # 開對話框前一次、存完一次
    assert logs == ["[OK] [Yvonne] 策略參數已儲存：SR=70%  SP500=40%  Skew LS=30%  "
                    "Leverage=1.4x"]


def test_the_dialog_starts_from_the_file_as_read_now():
    config = Config({"alloc_sr": 0.60, "alloc_sp500": 0.50, "alloc_skewls": 0.10})
    dialogs = Dialogs(answers=[(0.60, 0.50, 0.20)])
    run(config, dialogs)
    assert dialogs.asked == [("Taylor", (0.60, 0.50, 0.10))]
    assert not dialogs.confirms[0][2]                             # Skew LS 已經開過


def test_a_read_failure_is_shown_without_opening_the_dialog():
    config = Config(read_error=OSError("accounts.json missing"))
    dialogs = Dialogs(answers=[(0.6, 0.5, 0.0)])
    assert run(config, dialogs) == []
    assert dialogs.asked == [] and config.saves == []
    assert dialogs.messages == [("策略參數", "[Taylor] 設定讀取失敗：accounts.json missing", True)]


@pytest.mark.parametrize("answers, confirms, asked_confirm", [
    ([], [], 0),                          # 對話框按取消
    ([(0.60, 0.50, 0.0)], [False], 1),    # 確認視窗按否
])
def test_cancelling_saves_nothing(answers, confirms, asked_confirm):
    config, dialogs = Config(), Dialogs(answers=answers, confirms=confirms)
    assert run(config, dialogs) == []
    assert len(dialogs.confirms) == asked_confirm and config.saves == [] and config.reads == 1


def test_an_unchanged_allocation_is_not_written():
    config, dialogs = Config(), Dialogs(answers=[(0.80, 0.50, 0.0)])
    assert run(config, dialogs) == ["[Taylor] 策略參數沒有變更"]
    assert dialogs.confirms == [] and config.saves == []


def test_above_250_percent_is_refused():
    config, dialogs = Config(), Dialogs(answers=[(1.0, 1.0, 0.55)])
    run(config, dialogs)
    assert config.saves == [] and dialogs.confirms == []
    (title, text, error), = dialogs.messages
    assert error and "250%" in text


def test_first_enable_of_skewls_asks_with_a_short_selling_warning():
    config, dialogs = Config(), Dialogs(answers=[(0.80, 0.50, 0.10)])
    run(config, dialogs)
    (title, text, warning), = dialogs.confirms
    assert warning and "放空 XLC" in text
    assert config.saves == [("Taylor", {"alloc_sr": 0.80, "alloc_sp500": 0.50,
                                        "alloc_skewls": 0.10})]


def test_a_save_failure_is_shown_and_nothing_is_read_again():
    config = Config(save_error=OSError("accounts.json is locked"))
    dialogs = Dialogs(answers=[(0.60, 0.50, 0.0)])
    logs = run(config, dialogs)
    assert logs == ["[X] [Taylor] 策略參數儲存失敗：accounts.json is locked"]
    assert dialogs.messages == [("策略參數", "[Taylor] 儲存失敗：accounts.json is locked", True)]
    assert config.reads == 1                                      # 卡片留舊值


def test_a_failed_read_after_saving_does_not_undo_the_save():
    config, dialogs = Config(), Dialogs(answers=[(0.60, 0.50, 0.0)])
    original = config.read

    def read(name):
        if config.saves:
            raise OSError("locked")
        return original(name)

    config.read = read
    logs = run(config, dialogs)
    assert config.saves and logs[0].startswith("[OK] [Taylor] 策略參數已儲存")


# ── Gateway 狀態轉換 ─────────────────────────────────────────────────

@pytest.mark.parametrize("name, previous, state, detail, expected", [
    # 第一次回報：不寫 log；遠端帳戶照樣換托盤色
    ("Taylor", None, "init", "", GatewayTransition()),
    ("Rosa", None, "online", "", GatewayTransition(update_tray=True)),
    # 狀態沒變：什麼都不做
    ("Rosa", "online", "online", "", GatewayTransition()),
    ("Taylor", "offline", "offline", "x", GatewayTransition()),
    # 離線：寫 log（帶原因）；遠端帳戶另外換托盤色、告訴值班
    ("Taylor", "online", "offline", "socket closed",
     GatewayTransition(log="[GW] Taylor 離線 — socket closed")),
    ("Rosa", "online", "offline", "",
     GatewayTransition(update_tray=True, remote_offline=True, log="[GW] Rosa 離線")),
    # 連上：從 init 第一次連上不寫，之後才寫
    ("Taylor", "init", "online", "", GatewayTransition()),
    ("Taylor", "offline", "online", "", GatewayTransition(log="[GW] Taylor 已連上")),
    ("Rosa", "connecting", "online", "", GatewayTransition(update_tray=True,
                                                          log="[GW] Rosa 已連上")),
    ("Taylor", "online", "connecting", "", GatewayTransition()),
])
def test_gateway_transition(name, previous, state, detail, expected):
    assert gateway_transition(name, previous, state, detail, remote_account="Rosa") == expected


def test_gateway_transition_without_a_remote_account_never_touches_the_tray():
    todo = gateway_transition("Rosa", "online", "offline", "", remote_account=None)
    assert todo == GatewayTransition(log="[GW] Rosa 離線")
