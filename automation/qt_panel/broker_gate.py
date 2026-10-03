"""新 Panel 的送單閘門與 paper 連線設定（#151，ADR-0012）。

**送單閘門**：新 Panel 所有會送單的路徑（手動單、Sync Pos、交給策略接管）
都用 `SendGate.connect(ctx)` 開 IB 連線，這是唯一的判斷點。閘門包在 Broker
（`broker.Broker`，#185）外面：連上 Gateway 後先讀它管理的帳號；並行模式下只要
有一個不是 paper（DU/DF 開頭）就立刻斷線並拒絕，呼叫端拿不到連線，也就不能
送單／撤單。做法沿用 `paper_verify_manual_orders` 的 paper-only 檢查。帳號清單是
連線時 Gateway 給的，同一條連線不會變，所以只查一次：在送單時才拒絕的話，
service 已經先寫了委託紀錄，會留下卡住的假委託。正式模式（切換工單 #154）才放行
實盤。閘門只判斷放不放行；送去哪、記在哪、通知誰是送單目標（`order_target`，
#183）決定的。

**paper 連線設定**：並行模式下 Trading 只接 paper Gateway，位置寫在 config.json 的
`qt_panel_paper`（使用者自己填）。這只是 paper 送單目標的連線設定（Gateway 位置與
帳號）；三個帳戶名稱都導向這個 paper 帳號是 paper 送單目標做的。

不 import PySide6。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from broker import PAPER_CLIENT_ID, Broker
from manual_orders import ManualOrderError

from qt_panel.mode import PanelMode

PAPER_PREFIXES = ("DU", "DF")
PAPER_CONFIG_KEY = "qt_panel_paper"
DEFAULT_PAPER_HOST = "127.0.0.1"
# paper Gateway 上的 clientId 規則（基準號預設 92、Sync Pos＋1、接管＋2）在 broker。


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


class SendGate:
    """新 Panel 唯一的送單連線入口：向 Broker 開連線，先過帳號檢查才交出去。"""

    def __init__(self, mode: PanelMode, broker: Broker | None = None):
        self._mode = mode
        self._broker = broker or Broker()

    def check(self, accounts: Iterable[str]) -> None:
        if self._mode is PanelMode.PARALLEL:
            problem = paper_only_problem(accounts)
            if problem:
                raise SendRefused(problem)

    def connect(self, ctx, *, timeout: float = 20):
        """依 ctx 連線並檢查帳號；拒絕時已斷線並拋 SendRefused。回傳 broker.Connection。"""
        return self._broker.connect(ctx, timeout=timeout, check=self.check)


@dataclass(frozen=True)
class PaperTarget:
    """config.json `qt_panel_paper`：paper Gateway 的位置、paper 帳號與基準 clientId。"""
    host: str
    port: int
    account: str
    client_id: int

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
        client_id = int(section.get("client_id", PAPER_CLIENT_ID))
    except (TypeError, ValueError):
        return None, f"{PAPER_CONFIG_KEY} 的 client_id 必須是整數。"
    host = str(section.get("host") or DEFAULT_PAPER_HOST).strip()
    return PaperTarget(host=host, port=port, account=account, client_id=client_id), ""
