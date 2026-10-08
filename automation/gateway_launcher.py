r"""
QuantCore — IB Gateway 啟動器（原生 Gateway，不透過 IBC）

2026-06-01 完全棄用 IBC，全部走原生 Gateway。每個帳戶獨立 Gateway 安裝
（multi-install model），相互不干擾。安裝資料夾是 accounts.json 的 `gateway_dir`
（#211；沒寫就用 account_utils 的現狀預設：Taylor C:\IB-TAYLOR\ibgateway、
Yvonne C:\IB-YIYI\ibgateway、Rosa C:\Jts\ibgateway\1041 — 10.41，尚未遷移）。

啟動方式：直接執行 ibgateway.exe，傳 -J-DjtsConfigDir=<settings_dir> 指定
TWS 設定路徑。三個都已啟用每日 Auto-restart（沿用 token 重連、免 IB Key）；
週日 ColdRestart、當機或電腦重開後需要重新輸入帳密 + 手機 IB Key，這時用
Panel 的「啟動 Gateway」（gateway_start.py，#121）一次開三個並填好帳密。

棄用 IBC 的原因：IBC 3.23.0 在 Gateway 10.41 的 UI scraper 找不到 Auto
Restart 控件（"could not find auto logoff/restart time controls"），加上
Issue #345 的 token-based re-login 卡死。Gateway 10.45 把 UI 加回來了，
直接用原生功能即可，IBC 反而是負擔。
"""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# ── 每帳戶獨立 Gateway 安裝路徑（accounts.json 的 gateway_dir，#211）──────────
# Settings dir = Gateway 安裝路徑（jts.ini / autorestart token 都在 install 內）。
# Rosa 也一樣：2026-09-29 查 launcher log，每次啟動的 jtsConfigDir 都是
# C:\Jts\ibgateway\1041；舊的 C:\Jts\gateway_b 是 IBC 時代的設定夾，已不用。


def gateway_dir(account_name: str) -> Path | None:
    """帳戶的 Gateway 安裝（＝設定）資料夾；不認得或沒設定就 None。
    accounts.json 讀不了或設定錯（例如兩個 remote_gateway）照實丟出，不當成不認得。"""
    from account_utils import load_capabilities
    wanted = str(account_name).upper()
    return next((c.gateway_dir for c in load_capabilities() if c.name.upper() == wanted), None)


def _gateway_dirs() -> list[tuple[str, Path]]:
    from account_utils import load_capabilities
    return [(c.name, c.gateway_dir) for c in load_capabilities() if c.can_start_gateway]

_CONFIG_DIR_FLAG = "-J-DjtsConfigDir="


def _same_dir(a: str | Path, b: Path) -> bool:
    return str(a).rstrip(r"\/").lower() == str(b).rstrip(r"\/").lower()


def live_account_for_cmdline(cmdline: list[str]) -> str | None:
    r"""執行中 Gateway 的 command line → 實盤帳戶名（accounts.json 的 name），不是就 None。

    先看 jtsConfigDir，對不上再看 exe 所在的安裝資料夾：用舊設定夾或從開始選單
    直接開的實盤 Gateway 也要算到（寧可多擋，不可重複開）。
    paper Gateway（C:\IB-Paper Trade）不屬於任何實盤帳戶。
    """
    if not cmdline:
        return None
    config_dir = next((arg[len(_CONFIG_DIR_FLAG):] for arg in cmdline
                       if arg.startswith(_CONFIG_DIR_FLAG)), None)
    dirs = _gateway_dirs()
    if config_dir is not None:
        for account, settings_dir in dirs:
            if _same_dir(config_dir, settings_dir):
                return account
    install_dir = Path(cmdline[0]).parent
    for account, install in dirs:
        if _same_dir(install_dir, install):
            return account
    return None


def _restore_exe(install: Path):
    """Gateway shutdown 後有時會把 ibgateway.exe 改名 ibgateway1.exe，啟動前還原。"""
    exe = install / "ibgateway.exe"
    renamed = install / "ibgateway1.exe"
    if renamed.exists() and not exe.exists():
        renamed.rename(exe)


def start_gateway(account_name: str) -> subprocess.Popen | None:
    """
    啟動指定帳戶的 IB Gateway。

    啟動後會跳登入畫面，要輸入帳密 + 手機 IB Key（Panel「啟動 Gateway」會代填帳密）。
    之後 Gateway 每天 Auto-restart 免 IB Key，週日 ColdRestart 需要再登入一次。

    回傳 Popen 物件，None 表示失敗。
    """
    install = gateway_dir(account_name)          # 設定資料夾就是安裝資料夾
    account_name = account_name.upper()

    if not install:
        log.error(f"Unknown account: {account_name}")
        return None
    if not install.exists():
        log.error(f"Gateway install dir not found: {install}")
        return None

    _restore_exe(install)
    gateway_exe = install / "ibgateway.exe"
    if not gateway_exe.exists():
        log.error(f"ibgateway.exe not found in {install}")
        return None

    cmd = [
        str(gateway_exe),
        f"-J-DjtsConfigDir={install}",
    ]

    proc = subprocess.Popen(cmd)
    log.info(
        f"Gateway [{account_name}] started, PID={proc.pid}, "
        f"install={install}, settings={install}"
    )
    return proc


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("account", help="Account name in accounts.json (e.g. Taylor)")
    args = parser.parse_args()
    start_gateway(args.account)
