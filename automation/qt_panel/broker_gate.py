"""新 Panel 的送單閘門與 paper 連線設定（#151，ADR-0012）。

**送單閘門**：新 Panel 所有會送單的路徑（手動單、之後的 Sync Pos、交給策略接管）
都用 `SendGate.broker()` 建 IB 連線，這是唯一的判斷點。連上 Gateway 後先讀它
管理的帳號；並行模式下只要有一個不是 paper（DU/DF 開頭）就立刻斷線並拒絕，
沒通過檢查的連線一律不能送單／撤單。做法沿用 `paper_verify_manual_orders` 的
paper-only 檢查。帳號清單是連線時 Gateway 給的，同一條連線不會變，所以只查一次：
在 placeOrder 才拒絕的話，service 已經先寫了委託紀錄，會留下卡住的假委託。
正式模式（切換工單 #154）才放行實盤。

**paper 連線設定**：並行模式下 Trading 只接 paper Gateway，位置寫在 config.json 的
`qt_panel_paper`（使用者自己填），三個帳戶名稱都導向這一個 paper 帳號。

不 import PySide6。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Callable, Iterable, Mapping

from manual_orders import ManualOrderError

from qt_panel.mode import PanelMode

PAPER_PREFIXES = ("DU", "DF")
PAPER_CONFIG_KEY = "qt_panel_paper"
DEFAULT_PAPER_HOST = "127.0.0.1"
# paper Gateway 上的 clientId；paper_verify_manual_orders 預設用 91，錯開才能同時開。
# IB 只允許同一個 clientId 撤自己的單，所以固定不變。
DEFAULT_PAPER_CLIENT_ID = 92
# Sync Pos 在 paper 上用下一個 clientId：和手動單的對帳連線同時存在也不會互踢
SYNC_CLIENT_OFFSET = 1


class SendRefused(ManualOrderError):
    """送單閘門拒絕這條連線；訊息直接顯示在畫面上。"""


def paper_only_problem(accounts: Iterable[str]) -> str:
    """Gateway 管理的帳號全部是 paper 時回傳空字串，否則回傳給畫面的說明。"""
    accounts = [str(item).strip() for item in accounts or ()]
    if accounts and all(item.upper().startswith(PAPER_PREFIXES) for item in accounts):
        return ""
    shown = "、".join(accounts) if accounts else "（沒有回報任何帳號）"
    return (f"送單閘門拒絕：並行模式只放行 paper 帳號（DU/DF 開頭），"
            f"這個 Gateway 管理的帳號是 {shown}。已斷線，沒有送出任何單。"
            f"請確認 config.json 的 {PAPER_CONFIG_KEY} port 指向 paper Gateway。")


class _GatedBroker:
    """包住一條 IB 連線：connect 後檢查帳號，沒通過就不能送單／撤單。其餘原樣轉交。"""

    def __init__(self, inner, gate: "SendGate"):
        self._inner = inner
        self._gate = gate
        self._checked = False

    def connect(self, *args, **kwargs):
        result = self._inner.connect(*args, **kwargs)
        try:
            self._gate.check(self._inner.managedAccounts())
        except Exception:
            self._inner.disconnect()
            raise
        self._checked = True
        return result

    def _require_checked(self) -> None:
        if not self._checked:
            raise SendRefused("送單閘門拒絕：連線尚未通過帳號檢查，沒有送出任何單。")

    def placeOrder(self, contract, order):
        self._require_checked()
        return self._inner.placeOrder(contract, order)

    def cancelOrder(self, *args, **kwargs):
        self._require_checked()
        return self._inner.cancelOrder(*args, **kwargs)

    def disconnect(self):
        self._checked = False
        return self._inner.disconnect()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class SendGate:
    """新 Panel 唯一的送單連線工廠。"""

    def __init__(self, mode: PanelMode, broker_factory: Callable[[], object]):
        self._mode = mode
        self._factory = broker_factory

    def check(self, accounts: Iterable[str]) -> None:
        if self._mode is PanelMode.PARALLEL:
            problem = paper_only_problem(accounts)
            if problem:
                raise SendRefused(problem)

    def broker(self):
        return _GatedBroker(self._factory(), self)


@dataclass(frozen=True)
class PaperTarget:
    host: str
    port: int
    account: str
    client_id: int

    def account_ctx(self, name: str, _role: int) -> SimpleNamespace:
        """ManualOrderService 的 account_ctx_factory：任何帳戶名稱都導向 paper 帳號。"""
        return SimpleNamespace(name=name, account=self.account, host=self.host,
                               port=self.port, client_id=self.client_id)

    def sync_ctx(self, base):
        """Sync Pos 的 AccountCtx：策略設定（Allocation 等）沿用該帳戶，
        連線改到 paper；不去實盤 Gateway 借報價、不帶 Telegram 設定、一定真的送（paper）。"""
        return replace(base, host=self.host, port=self.port, account=self.account,
                       client_id=self.client_id + SYNC_CLIENT_OFFSET, price_feed_port=None,
                       dry_run=False, tg_token="", tg_chat_id="")

    def describe(self) -> str:
        return f"paper 帳號 {self.account}（{self.host}:{self.port}）"


def load_paper_target(config: Mapping, accounts: Iterable[Mapping]
                      ) -> tuple[PaperTarget | None, str]:
    """讀 config.json 的 qt_panel_paper；回傳 (設定, 問題說明)，有問題時設定為 None。"""
    section = config.get(PAPER_CONFIG_KEY)
    example = (f'例如 "{PAPER_CONFIG_KEY}": {{"port": 4013, "account": "DU1234567"}}')
    if section is None:
        return None, f"尚未設定 paper 連線：請在 config.json 加上 {PAPER_CONFIG_KEY}。{example}"
    if not isinstance(section, Mapping):
        return None, f"config.json 的 {PAPER_CONFIG_KEY} 格式不對。{example}"
    account = str(section.get("account") or "").strip().upper()
    if not account:
        return None, f"{PAPER_CONFIG_KEY} 缺少 account（paper 帳號）。{example}"
    if not account.startswith(PAPER_PREFIXES):
        return None, f"{PAPER_CONFIG_KEY} 的 account 必須是 paper 帳號（DU/DF 開頭）。"
    try:
        port = int(section.get("port"))
    except (TypeError, ValueError):
        return None, f"{PAPER_CONFIG_KEY} 缺少有效的 port（paper Gateway 的 API port）。{example}"
    live = next((a["name"] for a in accounts if a.get("port") == port), None)
    if live is not None:
        return None, (f"{PAPER_CONFIG_KEY} 的 port {port} 是 {live} 的實盤 Gateway，"
                      "不能當 paper 連線。")
    try:
        client_id = int(section.get("client_id", DEFAULT_PAPER_CLIENT_ID))
    except (TypeError, ValueError):
        return None, f"{PAPER_CONFIG_KEY} 的 client_id 必須是整數。"
    host = str(section.get("host") or DEFAULT_PAPER_HOST).strip()
    return PaperTarget(host=host, port=port, account=account, client_id=client_id), ""
