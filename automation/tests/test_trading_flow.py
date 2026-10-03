"""
qt_panel.trading_flow（#188）— 手動單流程與可取消計算，不開 Qt、不連 IB。

從 interface 測：ManualOrderFlow 的事件（預覽、確認、撤單、修改、刷新）→ FlowView
（畫面該顯示什麼）與 service 收到的呼叫；CancellableJob 的開始／放棄／關閉 →
結果與進度有沒有交給畫面。背景執行用同步 runner，或由測試決定何時完成的 runner。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import manual_order_form as mof
from manual_order_form import ManualOrderForm
from qt_panel.trading_flow import CancellableJob, ManualOrderFlow, Status


# ── runners ──────────────────────────────────────────────────────────

def _call(work):
    try:
        return work(), None
    except Exception as exc:
        return None, str(exc) or type(exc).__name__


class InlineRunner:
    """背景工作立刻完成。"""

    def run(self, work, done):
        done(*_call(work))

    def post(self, fn):
        fn()


class HeldRunner:
    """背景工作排隊，測試呼叫 finish() 才完成；post 也先排隊。"""

    def __init__(self):
        self.jobs = []
        self.posted = []

    def run(self, work, done):
        self.jobs.append((work, done))

    def post(self, fn):
        self.posted.append(fn)

    def finish(self):
        work, done = self.jobs.pop(0)
        result = _call(work)
        for fn in self.posted:
            fn()
        self.posted.clear()
        done(*result)


# ── 手動單 ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Preview:
    confirmation_text: str = "買進 AAPL 5 股 @ 100 USD"
    primary_exchange: str = "NASDAQ"
    price_warning: str = ""


def _form(**changes):
    base = dict(account="Taylor", symbol="AAPL", input_mode=mof.SHARES_MODE,
                quantity="5", amount="", percent="", order_type="LMT", price="100",
                session="一般時段", action="買進", allow_short=False)
    return ManualOrderForm(**{**base, **changes})


def _row(**changes):
    row = {"order_ref": "R1", "account_name": "Taylor", "symbol": "AAPL",
           "action": "BUY", "quantity": 5, "limit_price": 100.0,
           "status": "ACCEPTED", "status_label": "IB accepted",
           "filled_quantity": 0, "avg_fill_price": None,
           "order_type": "LMT", "session": "REGULAR"}
    row.update(changes)
    return row


class FakeService:
    def __init__(self):
        self.calls = []
        self.preview_result = Preview()
        self.validate_result = None        # None = 與預覽相同
        self.fail = {}                     # 方法名 → 錯誤訊息
        self.rows = []
        self.cancel_result = None
        self.submit_status = "ACCEPTED"

    def _maybe_fail(self, name):
        if name in self.fail:
            raise RuntimeError(self.fail[name])

    def preview(self, account, symbol, quantity, limit_price, **kwargs):
        self.calls.append(("preview", account, symbol, quantity, limit_price))
        self._maybe_fail("preview")
        return self.preview_result

    def validate_preview(self, preview):
        self.calls.append(("validate",))
        self._maybe_fail("validate")
        return self.validate_result or preview

    def submit(self, preview):
        self.calls.append(("submit",))
        self._maybe_fail("submit")
        row = _row(status=self.submit_status, status_message="IB 拒單：價格超出範圍"
                   if self.submit_status == "REJECTED" else "")
        self.rows = [row]
        return row

    def cancel(self, order_ref):
        self.calls.append(("cancel", order_ref))
        self._maybe_fail("cancel")
        result = self.cancel_result or _row(order_ref=order_ref, status="CANCELLED",
                                            status_label="已取消")
        self.rows = [result]
        return result

    def refresh_status(self):
        self.calls.append(("refresh",))
        self._maybe_fail("refresh")
        return {"orders": list(self.rows), "errors": []}

    def list_orders(self, active_only=False):
        return [r for r in self.rows
                if not active_only or r["status"] not in mof.TERMINAL_STATUSES]

    def has_active_orders(self):
        return bool(self.list_orders(active_only=True))

    def has_pending_notifications(self):
        return False


class FakeDialogs:
    def __init__(self):
        self.answer = True
        self.confirms = []
        self.messages = []

    def confirm(self, title, text, warning=False):
        self.confirms.append((title, text, warning))
        return self.answer

    def show_message(self, title, text, error=False):
        self.messages.append((title, text, error))


@pytest.fixture
def env():
    class Env:
        pass
    e = Env()
    e.service = FakeService()
    e.dialogs = FakeDialogs()
    e.form = _form()
    e.logs = []
    e.views = []

    def make(runner=None):
        e.runner = runner or InlineRunner()
        e.flow = ManualOrderFlow(
            e.service, runner=e.runner, dialogs=e.dialogs,
            read_form=lambda: e.form, load_form=lambda f: setattr(e, "form", f),
            log=e.logs.append, on_change=e.views.append, target_text="paper 帳號 DU001")
        return e.flow
    e.make = make
    return e


def _calls(env):
    return [c[0] for c in env.service.calls]


# 預覽

def test_preview_shows_the_confirmation_and_enables_confirm(env):
    flow = env.make()
    flow.preview()

    assert env.service.calls == [("preview", "Taylor", "AAPL", 5, 100.0)]
    view = flow.view
    assert view.form_status == Status("IB 合約已確認：NASDAQ")
    assert view.preview == Status("買進 AAPL 5 股 @ 100 USD", "text")
    assert (view.busy, view.can_confirm) == (False, True)


def test_invalid_field_is_explained_without_contacting_ib(env):
    env.form = _form(quantity="abc")
    flow = env.make()
    flow.preview()

    assert env.service.calls == []
    assert env.dialogs.messages and env.dialogs.messages[0][2] is True
    assert flow.view.busy is False


def test_preview_error_is_shown_in_red_and_nothing_can_be_confirmed(env):
    env.service.fail["preview"] = "策略 Universe 標的不能手動下單"
    flow = env.make()
    flow.preview()

    assert flow.view.form_status == Status("無法預覽")
    assert flow.view.preview == Status("策略 Universe 標的不能手動下單", "red")
    assert flow.view.can_confirm is False


def test_price_warning_turns_the_preview_yellow_and_the_dialog_warns(env):
    env.service.preview_result = Preview(price_warning="限價偏離前收 12%")
    flow = env.make()
    flow.preview()
    assert flow.view.preview.key == "yellow"
    flow.confirm()
    assert env.dialogs.confirms[0][2] is True


def test_while_busy_buttons_wait_and_a_second_preview_is_ignored(env):
    flow = env.make(HeldRunner())
    flow.preview()
    assert flow.view.busy and not flow.view.can_confirm
    flow.preview()
    assert len(env.runner.jobs) == 1
    env.runner.finish()
    assert not flow.view.busy and flow.view.can_confirm


def test_changing_a_field_after_preview_requires_a_new_preview(env):
    flow = env.make()
    flow.preview()
    flow.form_changed()

    assert flow.view.can_confirm is False
    assert flow.view.preview == Status("欄位已變更，請重新預覽委託。", "text")
    flow.confirm()
    assert "validate" not in _calls(env)


# 再次核對 → 確認 → 送出

def test_confirm_rechecks_with_ib_then_sends_after_the_dialog(env):
    flow = env.make()
    flow.preview()
    flow.confirm()

    assert _calls(env) == ["preview", "validate", "submit", "refresh"]
    title, prompt, _warning = env.dialogs.confirms[0]
    assert title == "再次核對委託"
    assert "送往：paper 帳號 DU001" in prompt and prompt.endswith("確認送出這筆委託？")
    assert flow.view.form_status == Status("Taylor AAPL：IB accepted", "green")
    assert flow.view.can_confirm is False                  # 同一份預覽只能送一次
    assert env.logs == ["[>] Manual order [Taylor] BUY 5sh AAPL LMT 100.0 USD REGULAR — ACCEPTED"]
    assert [r["order_ref"] for r in flow.view.orders] == ["R1"]


def test_declining_the_confirm_dialog_sends_nothing(env):
    env.dialogs.answer = False
    flow = env.make()
    flow.preview()
    flow.confirm()

    assert "submit" not in _calls(env)
    assert flow.view.can_confirm is True                   # 預覽仍有效，可以再按


def test_ib_data_changed_since_preview_invalidates_it(env):
    flow = env.make()
    flow.preview()
    env.service.validate_result = Preview(confirmation_text="持倉已變更")
    flow.confirm()

    assert "submit" not in _calls(env) and env.dialogs.confirms == []
    assert flow.view.form_status == Status("預覽已失效，請重新預覽")
    assert flow.view.preview == Status("資料在預覽後變更，請重新預覽", "red")
    assert flow.view.can_confirm is False


def test_recheck_error_invalidates_the_preview_with_the_reason(env):
    flow = env.make()
    flow.preview()
    env.service.fail["validate"] = "送單閘門拒絕：並行模式只放行 paper 帳號"
    flow.confirm()

    assert "submit" not in _calls(env)
    assert flow.view.preview == Status("送單閘門拒絕：並行模式只放行 paper 帳號", "red")


def test_form_edited_while_rechecking_needs_a_new_preview(env):
    flow = env.make(HeldRunner())
    flow.preview()
    env.runner.finish()
    flow.confirm()
    env.form = _form(price="101")                          # 核對進行中改了價格
    env.runner.finish()

    assert env.dialogs.confirms == [] and "submit" not in _calls(env)
    assert flow.view.preview == Status("欄位已變更，請重新預覽委託。", "text")


def test_submit_failure_and_ib_rejection_are_red(env):
    flow = env.make()
    flow.preview()
    env.service.fail["submit"] = "送單前重新檢查：已有未結案手動單"
    flow.confirm()
    assert flow.view.form_status == Status("送單前重新檢查未通過")
    assert flow.view.preview == Status("送單前重新檢查：已有未結案手動單", "red")

    env.service.fail.clear()
    env.service.submit_status = "REJECTED"
    flow.preview()
    flow.confirm()
    assert flow.view.form_status == Status("Taylor AAPL：IB 拒單：價格超出範圍", "red")


# 撤單

def test_cancel_marks_the_row_pending_until_ib_confirms(env):
    env.service.rows = [_row()]
    flow = env.make(HeldRunner())
    flow.start()
    env.runner.finish()                                    # 開啟時的刷新
    flow.cancel(flow.row("R1"))

    assert env.dialogs.confirms[0][0] == "取消手動委託"
    assert flow.view.busy
    assert flow.view.orders[0]["status_label"] == "取消確認中"
    assert flow.view.order_status == Status("已記錄取消要求，等待 IB 確認…")
    env.runner.finish()
    assert ("cancel", "R1") in env.service.calls
    assert flow.view.order_status == Status("已取消")
    assert flow.view.orders == ()                          # 已結案：不再列出


def test_cancel_declined_or_while_busy_does_nothing(env):
    env.service.rows = [_row()]
    env.dialogs.answer = False
    flow = env.make()
    flow.start()
    flow.cancel(flow.row("R1"))
    assert "cancel" not in _calls(env)


def test_unsettled_cancel_keeps_the_order_open_and_refreshes(env):
    env.service.rows = [_row()]
    env.service.cancel_result = _row(status="CANCEL_UNKNOWN", status_label="取消結果待確認",
                                     status_message="IB 沒有回覆取消結果")
    flow = env.make()
    flow.start()
    env.service.calls.clear()
    flow.cancel(flow.row("R1"))

    assert _calls(env) == ["cancel", "refresh"]
    assert flow.view.order_status.key == "text_dim"        # 刷新後的狀態
    assert flow.can_cancel(flow.row("R1")) is False        # 未確定前不能再撤


def test_cancel_error_is_shown_and_the_list_reloads(env):
    env.service.rows = [_row()]
    env.service.fail["cancel"] = "IB 連線中斷"
    flow = env.make()
    flow.start()
    flow.cancel(flow.row("R1"))

    assert flow.view.order_status == Status("取消結果待確認：IB 連線中斷", "red")
    assert flow.view.busy is False


# 修改 = 先撤再送

def test_modify_first_loads_the_selected_order_into_the_form(env):
    env.service.rows = [_row(symbol="MSFT")]
    flow = env.make()
    flow.start()
    flow.modify(flow.row("R1"))

    assert env.form.symbol == "MSFT"
    assert "cancel" not in _calls(env)
    assert flow.view.form_status == Status("已載入選定委託；調整價格或股數後再次按修改。")


def test_modify_cancels_then_offers_the_remaining_shares_after_a_partial_fill(env):
    env.service.rows = [_row()]
    env.service.cancel_result = _row(status="CANCELLED", status_label="已取消",
                                     filled_quantity=2)
    flow = env.make()
    flow.start()
    env.form = _form(price="99")                           # 同一筆委託，改價格
    flow.modify(flow.row("R1"))

    assert env.dialogs.confirms[-1][0] == "取消後修改手動委託"
    assert ("cancel", "R1") in env.service.calls
    assert "submit" not in _calls(env)                     # 不會自動送出新委託
    assert (env.form.quantity, env.form.price) == ("3", "99.0")   # 剩 3 股、新價格
    assert flow.view.form_status == Status("IB 已確認取消；請預覽並再次確認新委託。")
    assert "期間成交 2 股" in flow.view.preview.text
    assert flow.view.can_confirm is False


def test_modify_waits_while_the_cancel_is_unsettled_then_offers_the_new_order(env):
    env.service.rows = [_row()]
    env.service.cancel_result = _row(status="CANCEL_PENDING", status_label="取消確認中")
    flow = env.make()
    flow.start()
    env.form = _form(price="99")
    flow.modify(flow.row("R1"))
    assert env.form.price == "99"                          # 還在等 IB，不載入新委託

    env.service.rows = [_row(status="CANCELLED", status_label="已取消")]
    flow.refresh()
    assert env.form.quantity == "5" and env.form.price == "99.0"
    assert flow.view.form_status == Status("IB 已確認取消；請預覽並再次確認新委託。")


def test_modify_does_not_replace_an_order_that_filled_instead(env):
    env.service.rows = [_row()]
    env.service.cancel_result = _row(status="FILLED", status_label="已成交", filled_quantity=5)
    flow = env.make()
    flow.start()
    env.form = _form(price="99")
    flow.modify(flow.row("R1"))

    assert env.form.price == "99"                          # 沒有載入新委託
    assert "不會送出修改後委託" in flow.view.preview.text


def test_market_orders_cannot_be_modified(env):
    env.service.rows = [_row(order_type="MKT", limit_price=None)]
    flow = env.make()
    flow.start()
    assert flow.can_modify(flow.row("R1")) is False
    flow.modify(flow.row("R1"))
    assert "cancel" not in _calls(env)


# 刷新與生命週期

def test_start_refreshes_only_when_there_is_work_and_poll_skips_while_refreshing(env):
    flow = env.make(HeldRunner())
    flow.start()
    assert env.runner.jobs == []                           # 沒有未結案委託

    env.service.rows = [_row()]
    flow.poll()
    assert flow.view.refreshing and flow.view.order_status == Status("正在向 IB 查詢手動委託狀態…")
    flow.poll()
    assert len(env.runner.jobs) == 1
    env.runner.finish()
    assert not flow.view.refreshing
    assert flow.view.order_status == Status("狀態已刷新")


def test_refresh_error_is_red(env):
    env.service.rows = [_row()]
    env.service.fail["refresh"] = "Gateway 沒回應"
    flow = env.make()
    flow.refresh()
    assert flow.view.order_status == Status("IB 狀態查詢失敗：Gateway 沒回應", "red")


def test_results_after_stop_are_dropped(env):
    flow = env.make(HeldRunner())
    flow.preview()
    flow.stop()
    env.runner.finish()
    assert flow.view.can_confirm is False
    flow.refresh()
    assert len(env.runner.jobs) == 0


# ── 可取消計算 ───────────────────────────────────────────────────────

@pytest.fixture
def job_env():
    class Env:
        pass
    e = Env()
    e.runner = HeldRunner()
    e.job = CancellableJob(e.runner)
    e.progress, e.done, e.settled = [], [], []

    def start(result="plan", error=None):
        def work(progress):
            progress("  Computing SR weights...")
            if error:
                raise RuntimeError(error)
            return result
        return e.job.start(work, e.progress.append,
                           lambda r, err: e.done.append((r, err)),
                           on_settled=lambda: e.settled.append(True))
    e.start = start
    return e


def test_job_delivers_progress_and_result(job_env):
    assert job_env.start()
    assert job_env.job.computing and job_env.job.worker_running
    assert job_env.start() is False                        # 一次只跑一個
    job_env.runner.finish()

    assert job_env.progress == ["  Computing SR weights..."]
    assert job_env.done == [("plan", None)]
    assert not job_env.job.computing and job_env.job.can_start


def test_job_error_goes_to_on_done(job_env):
    job_env.start(error="IB 連線失敗")
    job_env.runner.finish()
    assert job_env.done == [(None, "IB 連線失敗")]


def test_discarded_job_result_and_progress_are_dropped(job_env):
    job_env.start()
    assert job_env.job.discard()
    assert not job_env.job.computing
    assert not job_env.job.can_start                       # 背景還連著 IB：不能重算
    assert job_env.start() is False
    job_env.runner.finish()

    assert job_env.done == [] and job_env.progress == []
    assert job_env.settled == [True]                       # 畫面可以重新開放「計算」
    assert job_env.job.can_start


def test_discard_without_a_running_job_does_nothing(job_env):
    assert job_env.job.discard() is False


def test_recompute_after_the_worker_ends_only_delivers_the_new_result(job_env):
    job_env.start(result="old")
    job_env.job.discard()
    job_env.runner.finish()
    job_env.start(result="new")
    job_env.runner.finish()
    assert job_env.done == [("new", None)]


def test_closed_job_drops_everything_and_cannot_start(job_env):
    job_env.start()
    job_env.job.close()
    job_env.runner.finish()
    assert job_env.done == [] and job_env.progress == []
    assert job_env.start() is False
