"""
QuantCore — IBC Gateway 啟動器
從 Control Panel 或命令列啟動指定帳戶的 IB Gateway（透過 IBC）。
"""

import subprocess, sys, os, logging
from pathlib import Path

log = logging.getLogger(__name__)

JAVA_EXE = r"c:\users\kelvi\appdata\local\programs\common\i4j_jres\oda-jk0qgtemvssflllp\17.0.10.0.101-zulu_64\bin\java.exe"
GATEWAY_JARS = Path(r"C:\Jts\ibgateway\1041\jars")
GATEWAY_I4J = Path(r"C:\Jts\ibgateway\1041\.install4j\i4jruntime.jar")
IBC_JAR = Path(r"C:\IBC\IBC.jar")
GATEWAY_EXE = Path(r"C:\Jts\ibgateway\1041\ibgateway.exe")

# 帳戶 → IBC config 對應
ACCOUNT_IBC_CONFIG = {
    "TAYLOR": Path(r"C:\Jts\gateway_a\ibc\config.ini"),
    "ROSA":   Path(r"C:\Jts\gateway_b\ibc\config.ini"),
    "YVONNE": Path(r"C:\Jts\gateway_c\ibc\config.ini"),
}

ACCOUNT_SETTINGS_DIR = {
    "TAYLOR": Path(r"C:\Jts\gateway_a"),
    "ROSA":   Path(r"C:\Jts\gateway_b"),
    "YVONNE": Path(r"C:\Jts\gateway_c"),
}

# 哪些帳戶用 IBC，哪些手動
IBC_MANAGED = {"TAYLOR", "YVONNE"}   # Taylor / Yvonne 用 IBC
MANUAL_MANAGED = {"ROSA"}            # Rosa 手動 Gateway


def _build_classpath() -> str:
    cp = ""
    for jar in sorted(GATEWAY_JARS.glob("*.jar")):
        cp += f";{jar}"
    cp += f";{GATEWAY_I4J};{IBC_JAR}"
    return cp


def _restore_exe():
    """IBC 會把 ibgateway.exe 改名，啟動前先恢復"""
    renamed = GATEWAY_EXE.parent / "ibgateway1.exe"
    if renamed.exists() and not GATEWAY_EXE.exists():
        renamed.rename(GATEWAY_EXE)


def start_gateway(account_name: str) -> subprocess.Popen | None:
    """
    啟動指定帳戶的 IB Gateway。
    IBC 管理的帳戶用 Java 啟動 IBC；手動帳戶用 ibgateway.exe 啟動。
    回傳 Popen 物件。
    """
    account_name = account_name.upper()
    settings_dir = ACCOUNT_SETTINGS_DIR.get(account_name)

    if not settings_dir:
        log.error(f"Unknown account: {account_name}")
        return None

    if account_name in IBC_MANAGED:
        return _start_ibc(account_name)
    else:
        return _start_native(account_name)


def _start_ibc(account_name: str) -> subprocess.Popen | None:
    """透過 IBC 啟動 Gateway"""
    config_ini = ACCOUNT_IBC_CONFIG.get(account_name)
    settings_dir = ACCOUNT_SETTINGS_DIR.get(account_name)

    if not config_ini or not config_ini.exists():
        log.error(f"IBC config not found: {config_ini}")
        return None

    _restore_exe()

    cp = _build_classpath()
    cmd = [
        str(JAVA_EXE),
        "-cp", cp,
        f"-DjtsConfigDir={settings_dir}",
        "-Xmx768m",
        "-Dsun.awt.nopixfmt=true",
        "-Dsun.java2d.noddraw=true",
        "-Dswing.boldMetal=false",
        # Java 17 模組封裝：解除 Gateway 需要的反射權限
        # （修復「Select second factor device」對話框 crash）
        "--add-opens=java.desktop/java.awt=ALL-UNNAMED",
        "--add-opens=java.desktop/javax.swing=ALL-UNNAMED",
        "--add-opens=java.desktop/sun.awt=ALL-UNNAMED",
        "--add-opens=java.base/java.lang=ALL-UNNAMED",
        "--add-opens=java.base/java.util=ALL-UNNAMED",
        "ibcalpha.ibc.IbcGateway",
        str(config_ini),
        "live",
    ]

    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0

    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        startupinfo=si,
    )
    log.info(f"IBC Gateway [{account_name}] started, PID={proc.pid}")
    return proc


def _start_native(account_name: str) -> subprocess.Popen | None:
    """直接啟動原生 Gateway（不透過 IBC）"""
    settings_dir = ACCOUNT_SETTINGS_DIR.get(account_name)

    if not GATEWAY_EXE.exists():
        _restore_exe()

    cmd = [
        str(GATEWAY_EXE),
        f"-J-DjtsConfigDir={settings_dir}",
    ]

    proc = subprocess.Popen(cmd)
    log.info(f"Native Gateway [{account_name}] started, PID={proc.pid}")
    return proc


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("account", help="Account name (A, B)")
    args = parser.parse_args()
    start_gateway(args.account)
