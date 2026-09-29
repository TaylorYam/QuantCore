"""Dashboard 畫面（#147：Gateway 狀態；其餘區塊在 #148 補齊）。"""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from qt_panel import theme
from qt_panel.widgets import Card, dot, label, set_dot_color, visible_text

WATCH_NOTE = "並行模式：值班由 Tk Panel 執行，新 Panel 不值班"


class GatewayCard(Card):
    def __init__(self, name: str, port):
        super().__init__()
        self.name = name
        self.state = "init"
        self._port = port

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(dot(theme.ACCOUNT_COLOR.get(name, theme.NAVY), 8))
        top.addWidget(label(name, name="MedNum"))
        top.addStretch(1)
        self._dot = dot(theme.GATEWAY_COLOR["init"])
        self._state = label()
        top.addWidget(self._dot)
        top.addWidget(self._state)
        self.body.addLayout(top)
        self._detail = label(name="Faint")
        self.body.addWidget(self._detail)
        self.set_state("init", "")

    def set_state(self, state: str, detail: str) -> None:
        self.state = state
        color = theme.GATEWAY_COLOR.get(state, theme.TEXT_FAINT)
        set_dot_color(self._dot, color)
        self._state.setText(theme.GATEWAY_TEXT.get(state, state))
        self._state.setStyleSheet(f"color: {color};")
        parts = [f"port {self._port}"] if self._port else []
        if detail:
            parts.append(detail)
        self._detail.setText(" · ".join(parts))


class Dashboard(QWidget):
    def __init__(self, accounts: list[dict]):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        root.addWidget(label("GATEWAY", name="SectionTitle"))
        row = QHBoxLayout()
        row.setSpacing(12)
        self._cards: dict[str, GatewayCard] = {}
        for acct in accounts:
            card = GatewayCard(acct["name"], acct.get("port"))
            self._cards[acct["name"]] = card
            row.addWidget(card, 1)
        root.addLayout(row)

        watch = Card("值班")
        self._watch_note = label(WATCH_NOTE, name="Dim")
        watch.body.addWidget(self._watch_note)
        root.addWidget(watch)
        root.addStretch(1)

    def set_gateway_state(self, name: str, state: str, detail: str) -> None:
        card = self._cards.get(name)
        if card is not None:
            card.set_state(state, detail)

    def gateway_names(self) -> list[str]:
        return list(self._cards)

    def gateway_state(self, name: str) -> str:
        return self._cards[name].state

    def gateway_text(self, name: str) -> str:
        return visible_text(self._cards[name])

    def watch_note(self) -> str:
        return self._watch_note.text()
