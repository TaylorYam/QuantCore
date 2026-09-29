"""Gateway 啟動（#121）的真實 Windows 實作：gateway_start.Ports 的四個外部 I/O。

- 正在執行的 Gateway：psutil 列 ibgateway*.exe，用 command line 對應實盤帳戶
- 認證管理員：advapi32 CredReadW / CredWriteW（generic credential，存在本機）
- 開啟 Gateway：gateway_launcher.start_gateway
- 填登入視窗：找該帳戶 Gateway process 的登入視窗 → 叫到最前面 → 打字
  帳號、Tab、密碼。不按 Enter／登入，也不動 Live/Paper、IB API 選項。

Gateway 是 Java Swing 視窗，裡面的欄位 Windows UI automation 看不到，所以只能
靠鍵盤輸入。打字用 SendInput 的 Unicode 事件，繞過中文輸入法。

不新增套件：全部 ctypes + 既有的 psutil。這支檔案離線測試不跑（會碰真的
認證管理員與視窗）；驗收 = 使用者下一次週日 ColdRestart。
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from typing import Callable

import psutil

from gateway_launcher import live_account_for_cmdline, start_gateway
from gateway_start import Credentials, LaunchFailed, WindowNotFound

log = logging.getLogger(__name__)

# 冷開 Gateway 到登入視窗出現要十幾秒到一分鐘；三個同時開會更慢
LOGIN_WINDOW_TIMEOUT_SEC = 120
# 視窗剛出現時 Swing 還在畫欄位、還沒把焦點放到帳號欄
LOGIN_WINDOW_SETTLE_SEC = 2.0
_POLL_SEC = 0.5

CREDENTIAL_TARGET = "QuantCore/IBGateway/{account}"

# ── advapi32：認證管理員 ─────────────────────────────────────────────

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32():
    lib = ctypes.WinDLL("advapi32", use_last_error=True)
    lib.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    lib.CredReadW.restype = wintypes.BOOL
    lib.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    lib.CredWriteW.restype = wintypes.BOOL
    lib.CredFree.argtypes = [ctypes.c_void_p]
    lib.CredFree.restype = None
    return lib


def read_credentials(account: str) -> Credentials | None:
    """從認證管理員讀該帳戶的 Gateway 帳密；沒存過回 None。"""
    lib = _advapi32()
    pcred = ctypes.POINTER(_CREDENTIALW)()
    target = CREDENTIAL_TARGET.format(account=account)
    if not lib.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
        err = ctypes.get_last_error()
        if err == _ERROR_NOT_FOUND:
            return None
        raise OSError(err, f"讀取 {account} 的 Gateway 帳密失敗")
    try:
        cred = pcred.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return Credentials(cred.UserName or "", blob.decode("utf-16-le"))
    finally:
        lib.CredFree(pcred)


def write_credentials(account: str, creds: Credentials) -> None:
    """把該帳戶的 Gateway 帳密存進認證管理員（覆蓋舊的）。"""
    lib = _advapi32()
    blob = creds.password.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob) if blob else None
    cred = _CREDENTIALW()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = CREDENTIAL_TARGET.format(account=account)
    cred.Comment = f"QuantCore — {account} IB Gateway 登入（#121）"
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)) if buf else None
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = creds.username
    if not lib.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), f"寫入 {account} 的 Gateway 帳密失敗")


# ── 執行中的 Gateway ─────────────────────────────────────────────────

def _live_gateway_pids() -> dict[int, str]:
    """pid → 實盤帳戶名；paper 或認不出來的 Gateway 不列入。"""
    found = {}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        name = (proc.info.get("name") or "").lower()
        if not name.startswith("ibgateway"):
            continue
        account = live_account_for_cmdline(proc.info.get("cmdline") or [])
        if account:
            found[proc.info["pid"]] = account
    return found


# ── user32：找視窗、叫到最前面、打字 ─────────────────────────────────

_user32 = ctypes.WinDLL("user32", use_last_error=True)

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_VK_TAB = 0x09
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_A = 0x41
_SW_RESTORE = 9


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    inp = _INPUT(type=_INPUT_KEYBOARD)
    inp.u.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _send(events: list[_INPUT]) -> None:
    arr = (_INPUT * len(events))(*events)
    sent = _user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):
        raise OSError(ctypes.get_last_error(), "SendInput 被擋下")


def _type_text(text: str) -> None:
    """Unicode 鍵盤事件逐字輸入，不經過輸入法。"""
    events = []
    raw = text.encode("utf-16-le")
    for i in range(0, len(raw), 2):
        unit = int.from_bytes(raw[i:i + 2], "little")
        events.append(_key(scan=unit, flags=_KEYEVENTF_UNICODE))
        events.append(_key(scan=unit, flags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))
    if events:
        _send(events)


def _press(vk: int) -> None:
    _send([_key(vk=vk), _key(vk=vk, flags=_KEYEVENTF_KEYUP)])


def _select_all() -> None:
    """Ctrl+A：欄位裡原本有字（Gateway 記得上次帳號）就整段取代。"""
    _send([_key(vk=_VK_CONTROL), _key(vk=_VK_A),
           _key(vk=_VK_A, flags=_KEYEVENTF_KEYUP),
           _key(vk=_VK_CONTROL, flags=_KEYEVENTF_KEYUP)])


def _window_title(hwnd) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _login_window_for(pids: set[int]):
    """這些 process 擁有的、看得見的 Gateway 視窗；還沒出現回 None。"""
    found = []

    def callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and "gateway" in _window_title(hwnd).lower():
            found.append(hwnd)
            return False
        return True

    _user32.EnumWindows(_EnumWindowsProc(callback), 0)
    return found[0] if found else None


def _bring_to_front(hwnd, account: str) -> None:
    # 背景執行緒不一定被允許搶前景；先送一下 Alt 讓 Windows 放行
    _press(_VK_MENU)
    _user32.ShowWindow(hwnd, _SW_RESTORE)
    _user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    if _user32.GetForegroundWindow() != hwnd:
        raise OSError(f"無法把 {account} 登入視窗叫到最前面")


def _ensure_front(hwnd, account: str) -> None:
    """打字前確認前景還是這個視窗，避免打進別的視窗。"""
    if _user32.GetForegroundWindow() != hwnd:
        raise OSError(f"{account} 登入視窗被切走了，停止輸入")


class WindowsPorts:
    """gateway_start.Ports 的真實實作。ask_credentials 由 Panel 提供（跳輸入框）。"""

    def __init__(self, ask_credentials: Callable[[str], Credentials | None]):
        self._ask = ask_credentials

    def running_live_accounts(self) -> set[str]:
        return set(_live_gateway_pids().values())

    def get_credentials(self, account: str) -> Credentials | None:
        return read_credentials(account)

    def set_credentials(self, account: str, creds: Credentials) -> None:
        write_credentials(account, creds)

    def ask_credentials(self, account: str) -> Credentials | None:
        return self._ask(account)

    def launch(self, account: str) -> None:
        if start_gateway(account.upper()) is None:
            raise LaunchFailed(f"{account} Gateway 開不起來（安裝資料夾或 ibgateway.exe 不見了，詳見 log）")

    def fill_login(self, account: str, creds: Credentials) -> None:
        deadline = time.monotonic() + LOGIN_WINDOW_TIMEOUT_SEC
        hwnd = None
        while time.monotonic() < deadline:
            pids = {pid for pid, a in _live_gateway_pids().items() if a == account}
            hwnd = _login_window_for(pids) if pids else None
            if hwnd:
                break
            time.sleep(_POLL_SEC)
        if not hwnd:
            raise WindowNotFound(f"{account} 登入視窗 {LOGIN_WINDOW_TIMEOUT_SEC} 秒內沒出現")

        time.sleep(LOGIN_WINDOW_SETTLE_SEC)
        _bring_to_front(hwnd, account)
        _select_all()
        _type_text(creds.username)
        _ensure_front(hwnd, account)
        _press(_VK_TAB)
        _select_all()
        _type_text(creds.password)
        log.info(f"Gateway [{account}] 登入視窗已填入帳密（未按登入）")
