"""登入完成的判斷：讀 Gateway 自己的 launcher.log（#133）。

測資是 2026-09-29 live 機器上 launcher.log 的實際行（只取判斷用得到的行）。
按下登入後，手機確認 IB Key 才會出現 tokenProtocol=5 那一行；Rosa 14:26 那次
在確認前 1.5 秒就先出現 tokenProtocol=0，不能算。
"""
from __future__ import annotations

from gateway_start_win import login_completed_in

SUBMIT = ("2026-09-29 15:19:58.067 [PL] INFO  [JTS-Login-14] - "
          "LauncherLoginThread.runLoginWithUI()[hotBackupStrategy=ON,loginAttempt=1,socketDelay=20000].\n")
EARLY_TOKEN = ("2026-09-29 14:26:45.966 INFO  [JTS-AuthDispatcherS2-18] - "
               "### onAuthenticationCompleted tokenProtocol=0; authenticated=true\n")
IB_KEY_DONE = ("2026-09-29 15:20:03.787 [PL] INFO  [JTS-AuthDispatcherS2-18] - "
               "### onAuthenticationCompleted tokenProtocol=5; authenticated=true\n")


def test_login_is_complete_once_ib_key_authentication_is_logged():
    assert login_completed_in(SUBMIT + IB_KEY_DONE)


def test_not_complete_while_waiting_for_ib_key():
    assert not login_completed_in(SUBMIT)


def test_the_early_token_line_before_ib_key_does_not_count():
    assert not login_completed_in(SUBMIT + EARLY_TOKEN)


def test_a_failed_authentication_does_not_count():
    assert not login_completed_in(IB_KEY_DONE.replace("authenticated=true", "authenticated=false"))
