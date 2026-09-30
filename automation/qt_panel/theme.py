"""顏色與樣式表（#145 原型定案：淺色、Noto Sans TC，見 #144 留言）。

淺灰頁面、白色卡片、深色字；Tk 版的深藍＋金色只當重點色。
"""

BG = "#f4f6f9"
SURFACE = "#ffffff"
SURFACE_2 = "#eef2f7"
SURFACE_HOVER = "#e5eaf1"
BORDER = "#dfe4ec"
BORDER_STRONG = "#c5cedb"
TEXT = "#0f172a"
TEXT_DIM = "#475569"
TEXT_FAINT = "#64748b"
NAVY = "#1e3a5f"
ACCENT = "#d97706"
GREEN = "#15803d"
RED = "#dc2626"
ORANGE = "#c2410c"
WARN_BG = "#ffedd5"
WARN_TEXT = "#9a3412"

ACCOUNT_COLOR = {"Taylor": "#d97706", "Rosa": "#0284c7", "Yvonne": "#9333ea"}
GATEWAY_COLOR = {"init": TEXT_FAINT, "connecting": ORANGE, "online": GREEN, "offline": RED}
GATEWAY_TEXT = {"init": "檢查中…", "connecting": "連線中…", "online": "線上", "offline": "離線"}

AMBER = "#b45309"   # 白底上看得清楚的黃（Tk 的 yellow）
BLUE = "#1d4ed8"

# portfolio_display 等共用模組回傳的 theme key → 新 Panel 的顏色
THEME_KEY_COLOR = {"text": TEXT, "text_dim": TEXT_DIM, "green": GREEN, "red": RED,
                   "yellow": AMBER, "orange": ORANGE, "blue": BLUE}


def color(key: str) -> str:
    """共用模組回傳的 theme key → 顏色；不認得的 key 用一般文字色。"""
    return THEME_KEY_COLOR.get(key, TEXT)


# 持倉歸屬（portfolio_display.owner_of）的顏色
OWNER_COLOR = {"SR": BLUE, "SkewLS": "#7c3aed", "SP500": GREEN, "手動": TEXT_DIM}

# 今日排程各狀態的顏色（文字見 dashboard_data.JOB_STATE_TEXT）
JOB_COLOR = {"pending": TEXT_FAINT, "missing": RED, "unfinished": RED,
             "ok": GREEN, "problem": ORANGE, "ran": GREEN}

UI_FONT = "Noto Sans TC"
MONO_FONT = "Consolas"

STYLESHEET = f"""
QWidget {{ background: transparent; color: {TEXT}; font-family: "{UI_FONT}"; font-size: 10pt; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 4px; }}
QMenu {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 14px; border-radius: 4px; }}
QMenu::item:selected {{ background: {SURFACE_HOVER}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}
/* 下拉清單是獨立的彈出視窗，透明背景在 Windows 原生樣式會變黑（#169） */
QComboBoxPrivateContainer {{ background: {SURFACE}; border: 1px solid {BORDER_STRONG}; }}
QComboBox QAbstractItemView {{
    background: {SURFACE}; color: {TEXT}; border: none; outline: 0;
    selection-background-color: {SURFACE_HOVER}; selection-color: {TEXT};
}}
QComboBox QAbstractItemView::item {{ padding: 4px 8px; color: {TEXT}; }}
QComboBox QAbstractItemView::item:selected {{ background: {SURFACE_HOVER}; color: {TEXT}; }}

#Header {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
#Brand {{ font-size: 14pt; font-weight: 700; color: {NAVY}; }}
#Clock {{ color: {TEXT_DIM}; }}
#Pill {{ border-radius: 10px; padding: 2px 10px; font-size: 9pt; font-weight: 600; }}

#Nav {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
#NavButton {{
    background: transparent; border: none; border-left: 3px solid transparent;
    color: {TEXT_DIM}; text-align: left; padding: 10px 16px; font-size: 10.5pt;
}}
#NavButton:hover {{ background: {SURFACE_HOVER}; color: {TEXT}; }}
#NavButton:checked {{ background: {SURFACE_2}; color: {TEXT}; border-left: 3px solid {ACCENT}; font-weight: 600; }}

#Card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; }}
#SectionTitle {{ color: {TEXT_DIM}; font-size: 9pt; font-weight: 600; letter-spacing: 0.5px; }}
#Dim {{ color: {TEXT_DIM}; }}
#Faint {{ color: {TEXT_FAINT}; font-size: 9pt; }}
#MedNum {{ font-size: 12pt; font-weight: 500; }}
#BigNum {{ font-size: 18pt; font-weight: 700; }}
#Card[clickable="true"]:hover {{ border-color: {BORDER_STRONG}; }}
#Card[selected="true"] {{ border: 2px solid {ACCENT}; }}
QPushButton#Chip {{ padding: 3px 12px; border-radius: 12px; font-size: 9pt; }}
QPushButton#Chip:checked {{ background: {NAVY}; border-color: {NAVY}; color: #ffffff; }}
QTableWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px;
    gridline-color: {SURFACE_2}; selection-background-color: {SURFACE_2}; selection-color: {TEXT};
}}
QHeaderView::section {{
    background: {SURFACE_2}; color: {TEXT_DIM}; border: none; padding: 4px 6px; font-size: 9pt;
}}
#Log {{
    background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 6px;
    font-family: "{MONO_FONT}"; font-size: 9pt; padding: 4px;
}}
#Placeholder {{ color: {TEXT_DIM}; font-size: 11pt; }}

/* 捲軸：細、圓角、沒有上下箭頭；平常淺灰，滑鼠移上去變深（#171） */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px 2px 2px 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0 2px 2px 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_STRONG}; border-radius: 4px; min-height: 36px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_STRONG}; border-radius: 4px; min-width: 36px; }}
QScrollBar::handle:hover, QScrollBar::handle:pressed {{ background: {TEXT_FAINT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QPushButton {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 6px;
    padding: 5px 14px; color: {TEXT};
}}
QPushButton:hover {{ background: {SURFACE_HOVER}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background: {SURFACE_2}; }}
QPushButton#Primary {{ background: {ACCENT}; border-color: {ACCENT}; color: #ffffff; font-weight: 600; }}
QPushButton#Primary:hover {{ background: #b45309; }}
QPushButton#Primary:disabled {{ background: {BORDER_STRONG}; border-color: {BORDER_STRONG}; }}
QPushButton#Small {{ padding: 2px 10px; font-size: 9pt; }}
"""
