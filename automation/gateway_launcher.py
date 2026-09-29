r"""
QuantCore — IB Gateway 啟動器（原生 Gateway，不透過 IBC）

2026-06-01 完全棄用 IBC，全部走原生 Gateway。每個帳戶獨立 Gateway 安裝
（multi-install model），相互不干擾：

    TAYLOR  C:\IB-TAYLOR\ibgateway\      (10.45)
    YVONNE  C:\IB-YIYI\ibgateway\        (10.45)
    ROSA    C:\Jts\ibgateway\1041\     (10.41，尚未遷移)

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

# ── 每帳戶獨立 Gateway 安裝路徑 ──────────────────────────────────────────────
# Taylor / Yvonne 已遷移到 Gateway 10.45 個別安裝。
# Rosa 暫留舊的 10.41 共享 binary，未來再遷移到 C:\IB-ROSA\ibgateway\。
LEGACY_GATEWAY_DIR = Path(r"C:\Jts\ibgateway\1041")

ACCOUNT_GATEWAY_DIR = {
    "TAYLOR": Path(r"C:\IB-TAYLOR\ibgateway"),  # 10.45 個別安裝
    "YVONNE": Path(r"C:\IB-YIYI\ibgateway"),    # 10.45 個別安裝
    "ROSA":   LEGACY_GATEWAY_DIR,               # 10.41 共享舊裝
}

# Settings dir = Gateway 安裝路徑（jts.ini / autorestart token 都在 install 內）。
# Rosa 也一樣（2026-09-29 依執行中 process 的 jtsConfigDir 查證；舊的
# C:\Jts\gateway_b 是 IBC 時代的設定夾，已不用）。
ACCOUNT_SETTINGS_DIR = {
    "TAYLOR": Path(r"C:\IB-TAYLOR\ibgateway"),
    "YVONNE": Path(r"C:\IB-YIYI\ibgateway"),
    "ROSA":   LEGACY_GATEWAY_DIR,
}

_CONFIG_DIR_FLAG = "-J-DjtsConfigDir="


def _same_dir(a: str | Path, b: Path) -> bool:
    return str(a).rstrip(r"\/").lower() == str(b).rstrip(r"\/").lower()


def live_account_for_cmdline(cmdline: list[str]) -> str | None:
    r"""執行中 Gateway 的 command line → 實盤帳戶名（Taylor/Rosa/Yvonne），不是就 None。

    以 jtsConfigDir 為準；沒帶（例如從開始選單直接開）就看 exe 所在的安裝資料夾。
    paper Gateway（C:\IB-Paper Trade）不屬於任何實盤帳戶。
    """
    if not cmdline:
        return None
    config_dir = next((arg[len(_CONFIG_DIR_FLAG):] for arg in cmdline
                       if arg.startswith(_CONFIG_DIR_FLAG)), None)
    folder = config_dir if config_dir is not None else str(Path(cmdline[0]).parent)
    for account, settings_dir in ACCOUNT_SETTINGS_DIR.items():
        if _same_dir(folder, settings_dir):
            return account.capitalize()
    return None


def _restore_exe(gateway_dir: Path):
    """Gateway shutdown 後有時會把 ibgateway.exe 改名 ibgateway1.exe，啟動前還原。"""
    exe = gateway_dir / "ibgateway.exe"
    renamed = gateway_dir / "ibgateway1.exe"
    if renamed.exists() and not exe.exists():
        renamed.rename(exe)


def start_gateway(account_name: str) -> subprocess.Popen | None:
    """
    啟動指定帳戶的 IB Gateway。

    第一次啟動會跳登入畫面，需手動輸入帳密 + IBKR Mobile 2FA。
    之後 Gateway 自己 daily auto-restart，週日 ColdRestart 需要再一次 2FA。

    回傳 Popen 物件，None 表示失敗。
    """
    account_name = account_name.upper()
    settings_dir = ACCOUNT_SETTINGS_DIR.get(account_name)
    gateway_dir = ACCOUNT_GATEWAY_DIR.get(account_name)

    if not gateway_dir:
        log.error(f"Unknown account: {account_name}")
        return None
    if not gateway_dir.exists():
        log.error(f"Gateway install dir not found: {gateway_dir}")
        return None

    _restore_exe(gateway_dir)
    gateway_exe = gateway_dir / "ibgateway.exe"
    if not gateway_exe.exists():
        log.error(f"ibgateway.exe not found in {gateway_dir}")
        return None

    cmd = [
        str(gateway_exe),
        f"-J-DjtsConfigDir={settings_dir}",
    ]

    proc = subprocess.Popen(cmd)
    log.info(
        f"Gateway [{account_name}] started, PID={proc.pid}, "
        f"install={gateway_dir}, settings={settings_dir}"
    )
    return proc


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("account", help="Account name (TAYLOR, ROSA, YVONNE)")
    args = parser.parse_args()
    start_gateway(args.account)
