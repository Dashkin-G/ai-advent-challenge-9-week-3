"""Интерфейс агента — десктопное окно: python -m app.gui

Окно ничего не знает про устройство агента: оно создаёт `Agent`, отдаёт ему строку
и показывает `reply.text` вместе с трассой шагов. Ни сборки запроса, ни памяти, ни
исполнения инструментов здесь нет — всё это внутри агента (app/agent.py), и в нём
нет ни одного импорта Qt. Проверить границу просто: любой новый интерфейс поверх
`Agent` не должен требовать правок в самом агенте.

Интерфейс на PySide6 (Qt): Qt рисует виджеты сам и стилизуется таблицей QSS —
поэтому тёмная тема получается полностью управляемой, вплоть до полос прокрутки.

Важная особенность: вызов модели блокирующий, а перерисовывает окно главный поток.
Поэтому обращение к агенту уходит в отдельный QThread, а результат возвращается
сигналом.

При запуске окно не создаёт агентов само: их поднимает `load_agents()` из
хранилища, поэтому после перезапуска на экране оказывается тот же агент с тем же
разговором. Про формат хранения окно по-прежнему ничего не знает — только про то,
что состояние где-то есть.
"""
import json
import logging
import re
import sys
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QKeySequence, QPainter, QPalette, QPen, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMenu, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea,
    QSizePolicy, QSlider, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from . import config, memory
from .agent import (
    DEFAULT_PROFILE, MEMORY_TURNS_MAX, Agent, AgentError, AgentProfile, AgentReply, load_agents,
    load_personas,
)
from .store import MAIN_BRANCH, Store

# В окне нужен диалог, а не логи вызовов — оставляем только предупреждения.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s [%(name)s] %(message)s")

# --- Палитра ---------------------------------------------------------------
BLACK = "#08090b"     # фон ленты
PANEL = "#0d0f13"     # боковая панель, шапка, полоса ввода
CARD = "#13161c"      # карточки и пузыри агента
LINE = "#1e232c"      # границы
TEXT = "#e8eaed"
MUTED = "#79818f"
ACCENT = "#4f8cff"
ACCENT2 = "#2b6bff"
ERR_BG = "#241417"
ERR_LINE = "#4a1f26"
ERR_TEXT = "#ff9d9d"
OK = "#2fbf6b"
WARN = "#e6b800"
SUMMARY = "#ff7eb6"   # суммаризация — сжатая память; свой цвет, чтобы отличался от памяти как есть
FACTS = "#3fc9b8"     # долговременная память «ключ — значение»: профиль, решения, знания
TASK = "#f0a35e"      # рабочая память — карточка текущей задачи
BRANCH = "#b48cff"    # ветки диалога и точки ветвления
PERSONA = "#b7e26b"   # профиль пользователя: персонализация поверх памяти

# Цвет слоя памяти. Краткосрочная — цветом обычной памяти: это она и есть, только
# названная по модели; у сжатой части внутри неё свой цвет (SUMMARY).
LAYER_COLORS = {"short": ACCENT, "working": TASK, "long": FACTS}

BUBBLE_ID = {
    "user": "bubbleUser", "agent": "bubbleAgent", "error": "bubbleError",
    "shadow": "bubbleShadow",   # теневой ответ «с полной историей» — только для сравнения
}

QSS = f"""
QWidget {{
    background: {BLACK};
    color: {TEXT};
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 14px;
}}
QFrame#sidebar {{ background: {PANEL}; border-right: 1px solid {LINE}; }}
QScrollArea#sidebarScroll, QWidget#sidebarInner {{ background: {PANEL}; }}
QWidget#sidebarInner QLabel {{ background: transparent; }}
QFrame#card {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 14px; }}
QFrame#header {{ background: {PANEL}; border-bottom: 1px solid {LINE}; }}
QFrame#composer {{ background: {PANEL}; border-top: 1px solid {LINE}; }}
QFrame#card QLabel, QFrame#header QLabel, QFrame#composer QLabel,
QFrame#sidebar QLabel {{ background: transparent; }}

QLabel#agentName {{ font-size: 17px; font-weight: 700; }}
QLabel#agentRole {{ color: {MUTED}; font-size: 12px; }}
QLabel#agentId {{ color: {MUTED}; font-family: Consolas, monospace; font-size: 11px; }}
QLabel#avatar {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT2}, stop:1 #7a5cff);
    border-radius: 22px; font-size: 20px;
}}
QLabel#section {{ color: {MUTED}; font-size: 10px; font-weight: 700; letter-spacing: 1px; }}
QLabel#note {{ color: {MUTED}; font-size: 11px; }}
QLabel#meta {{ color: {MUTED}; font-family: Consolas, monospace; font-size: 11px; }}
QLabel#system {{ color: {MUTED}; font-size: 12px; }}
QLabel#statValue {{ font-size: 20px; font-weight: 700; }}
QLabel#statLabel {{ color: {MUTED}; font-size: 11px; }}
QLabel#title {{ font-size: 15px; font-weight: 600; }}
QLabel#subtitle {{ color: {MUTED}; font-size: 12px; }}
QLabel#status {{ color: {MUTED}; font-size: 12px; }}

QFrame#bubbleUser {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT2}, stop:1 {ACCENT});
    border-radius: 16px;
}}
QFrame#bubbleUser QLabel {{ background: transparent; color: #ffffff; }}
QFrame#bubbleAgent {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 16px; }}
QFrame#bubbleAgent QLabel {{ background: transparent; }}
QFrame#bubbleError {{ background: {ERR_BG}; border: 1px solid {ERR_LINE}; border-radius: 16px; }}
QFrame#bubbleError QLabel {{ background: transparent; color: {ERR_TEXT}; }}
QFrame#bubbleShadow {{ background: {PANEL}; border: 1px dashed #3a4356; border-radius: 16px; }}
QFrame#bubbleShadow QLabel {{ background: transparent; color: #c2c7d0; }}
QFrame#stat {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 12px; }}
QFrame#stat QLabel {{ background: transparent; }}

QPushButton#send {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT2}, stop:1 {ACCENT});
    color: #ffffff; border: 0; border-radius: 12px; padding: 0 26px;
    font-size: 14px; font-weight: 700;
}}
QPushButton#send:hover {{ background: {ACCENT}; }}
QPushButton#send:disabled {{ background: #1b2330; color: {MUTED}; }}
QPushButton#ghost {{
    background: transparent; color: {TEXT}; border: 1px solid {LINE};
    border-radius: 10px; padding: 9px 12px; font-size: 13px;
}}
QPushButton#ghost:hover {{ background: {CARD}; border-color: #2c3441; }}
QPushButton#link {{
    background: transparent; border: 0; color: {ACCENT};
    font-size: 11px; font-family: Consolas, monospace; padding: 0;
}}
QPushButton#link:hover {{ color: #82adff; }}

QCheckBox {{ font-size: 13px; spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 5px;
    border: 1px solid #2c3441; background: {CARD};
}}
QCheckBox::indicator:checked {{ background: {ACCENT2}; border-color: {ACCENT2}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
/* Переключатель стратегий: круглые индикаторы того же цвета, что и флажки. */
QRadioButton {{ font-size: 13px; spacing: 8px; }}
QRadioButton::indicator {{
    width: 16px; height: 16px; border-radius: 8px;
    border: 1px solid #2c3441; background: {CARD};
}}
QRadioButton::indicator:checked {{ background: {ACCENT2}; border-color: {ACCENT2}; }}
QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
/* Английское название стратегии из задания — второй строкой под русской подписью. */
QLabel#strategyEn {{
    color: {MUTED}; font-family: Consolas, monospace; font-size: 11px; padding-left: 25px;
}}
/* Карта слоёв памяти в панели: название слоя и его вес, под ним — что в нём лежит. */
QLabel#layerHead {{ font-size: 12px; font-weight: 600; }}

QFrame#trace {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 14px; }}
QFrame#trace QLabel {{ background: transparent; }}
QLabel#traceLabel {{ color: {MUTED}; font-size: 10px; font-weight: 700; letter-spacing: 1px; }}
QLabel#planItem {{ font-size: 13px; }}
QLabel#stepHead {{ color: {ACCENT}; font-family: Consolas, monospace; font-size: 12px; }}
QLabel#stepHeadErr {{ color: {ERR_TEXT}; font-family: Consolas, monospace; font-size: 12px; }}
QLabel#summaryHead {{ color: {SUMMARY}; font-family: Consolas, monospace; font-size: 12px; }}
QLabel#factsHead {{ color: {FACTS}; font-family: Consolas, monospace; font-size: 12px; }}
QLabel#taskHead {{ color: {TASK}; font-family: Consolas, monospace; font-size: 12px; }}
QLabel#personaHead {{ color: {PERSONA}; font-family: Consolas, monospace; font-size: 12px; }}
/* Профиль пользователя в панели: название и его состав второй строкой. */
QLabel#personaName {{ color: {PERSONA}; font-size: 13px; font-weight: 600; }}
/* Строка задачи в шапке: рабочая память видна там же, где имя агента. */
QLabel#taskLine {{ color: {TASK}; font-size: 12px; }}
/* Метки веток в ленте: точка ветвления и место развилки. */
QLabel#marker {{ color: {BRANCH}; font-size: 12px; font-weight: 600; }}
/* Метка смены этапа: появляется в ленте в момент перехода, поэтому заметнее
   остальных — по ней в записи видно, где именно автомат сдвинулся. */
QLabel#stateMarker {{
    color: {TASK}; font-size: 12px; font-weight: 700;
    border: 1px solid {LINE}; border-radius: 8px; padding: 4px 10px;
}}
QFrame#stepResult {{ background: {BLACK}; border: 1px solid {LINE}; border-radius: 8px; }}
QFrame#stepResult QLabel {{
    background: transparent; color: #a9c39a;
    font-family: Consolas, monospace; font-size: 11px;
}}

QComboBox, QSpinBox {{
    background: {CARD}; border: 1px solid {LINE}; border-radius: 10px;
    padding: 8px 10px; font-size: 13px; selection-background-color: {ACCENT2};
}}
QComboBox:hover, QSpinBox:hover {{ border-color: #2c3441; }}
/* Поле, которое при этой стратегии не действует (период суммаризации), гасим. */
QSpinBox:disabled {{ color: #4a5260; border-color: {LINE}; }}
QLabel:disabled {{ color: #4a5260; }}
QComboBox::drop-down {{ border: 0; width: 22px; }}
/* Стрелки спинбокса система рисует светлыми — прячем, поле правится с клавиатуры. */
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: 0; }}
QComboBox QAbstractItemView {{
    background: {CARD}; border: 1px solid {LINE};
    selection-background-color: {ACCENT2}; outline: 0; padding: 4px;
}}
QTextEdit#input {{
    background: {CARD}; border: 1px solid {LINE}; border-radius: 12px;
    padding: 10px 12px; font-size: 14px; selection-background-color: {ACCENT2};
}}
QTextEdit#input:focus {{ border-color: {ACCENT2}; }}
QPlainTextEdit#raw {{
    background: {BLACK}; border: 1px solid {LINE}; border-radius: 10px;
    font-family: Consolas, monospace; font-size: 12px; color: #a9c39a;
}}
QTabWidget::pane {{ border: 0; }}
QTabBar::tab {{
    background: transparent; color: {MUTED}; padding: 7px 14px;
    border-bottom: 2px solid transparent; font-size: 12px;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom-color: {ACCENT2}; }}

QSlider::groove:horizontal {{ height: 4px; background: {LINE}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT2}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; background: {ACCENT};
}}
QSlider::handle:horizontal:hover {{ background: #7fb0ff; }}

QScrollArea {{ border: 0; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: #232a34; border-radius: 5px; min-height: 40px; }}
QScrollBar::handle:vertical:hover {{ background: #333c4a; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
QScrollBar::handle:horizontal {{ background: #232a34; border-radius: 5px; min-width: 40px; }}
QScrollBar::handle:horizontal:hover {{ background: #333c4a; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

QPushButton#newAgent {{
    background: {CARD}; color: {TEXT}; border: 1px solid {ACCENT2};
    border-radius: 10px; padding: 7px 14px; font-size: 13px; font-weight: 600;
}}
QPushButton#newAgent:hover {{ background: {ACCENT2}; }}

/* Полоса агентов над лентой: список растёт вбок, а не выдавливает панель вниз */
QWidget#agentBarRow {{ background: {PANEL}; border-bottom: 1px solid {LINE}; }}
QScrollArea#agentBar, QWidget#agentBarInner {{ background: {PANEL}; }}
QFrame#agentItem {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 10px; }}
QFrame#agentItem:hover {{ border-color: {ACCENT}; }}
QFrame#agentItemActive {{
    background: #182034; border: 1px solid {ACCENT2}; border-radius: 10px;
}}
QFrame#agentItem QLabel, QFrame#agentItemActive QLabel {{ background: transparent; }}
QLabel#agentItemName {{ font-size: 13px; font-weight: 600; }}
QLabel#agentItemSub {{ color: {MUTED}; font-size: 11px; }}

/* Полоса веток под полосой агентов: видна только в стратегии «ветки диалога» */
QWidget#branchBarRow {{ background: {BLACK}; border-bottom: 1px solid {LINE}; }}
QScrollArea#branchBar, QWidget#branchBarInner {{ background: {BLACK}; }}
QWidget#branchBarRow QLabel {{ background: transparent; }}
QLabel#branchCaption {{ color: {BRANCH}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
QFrame#branchItem {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 8px; }}
QFrame#branchItem:hover {{ border-color: {BRANCH}; }}
QFrame#branchItemActive {{ background: #211b33; border: 1px solid {BRANCH}; border-radius: 8px; }}
QFrame#branchItem QLabel, QFrame#branchItemActive QLabel {{ background: transparent; }}
QLabel#branchItemName {{ font-size: 12px; font-weight: 600; }}
QLabel#branchItemSub {{ color: {MUTED}; font-size: 11px; }}
QPushButton#branchAction {{
    background: transparent; color: {TEXT}; border: 1px solid {LINE};
    border-radius: 8px; padding: 5px 10px; font-size: 12px;
}}
QPushButton#branchAction:hover {{ border-color: {BRANCH}; background: {CARD}; }}

/* Полоса этапов задачи: конечный автомат рабочей памяти. Видна, только пока
   задача открыта. Текущий этап подсвечен, разрешённые переходы кликабельны,
   запрещённые приглушены — и запрет держит код, а не только цвет. */
QWidget#stateBarRow {{ background: {BLACK}; border-bottom: 1px solid {LINE}; }}
QWidget#stateBarRow QLabel {{ background: transparent; }}
QLabel#stateCaption {{ color: {TASK}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
QLabel#stateArrow {{ color: {MUTED}; font-size: 13px; }}
QLabel#stateProgress {{ color: {MUTED}; font-size: 11px; }}
QLabel#stateExpect {{ color: {TASK}; font-size: 11px; }}
QFrame#stateItem {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 8px; }}
QFrame#stateItemActive {{ background: #2a1f12; border: 1px solid {TASK}; border-radius: 8px; }}
QFrame#stateItemLocked {{ background: {BLACK}; border: 1px dashed {LINE}; border-radius: 8px; }}
/* Этап, на котором задача замерла: подсвечен как текущий, но пунктиром — работа
   стоит, а место в автомате сохранено. */
QFrame#stateItemPaused {{ background: {BLACK}; border: 1px dashed {TASK}; border-radius: 8px; }}
QFrame#stateItem QLabel, QFrame#stateItemActive QLabel {{ background: transparent; }}
QFrame#stateItemLocked QLabel {{ background: transparent; color: {MUTED}; }}
QFrame#stateItemPaused QLabel {{ background: transparent; color: {MUTED}; }}
QLabel#stateItemName {{ font-size: 12px; font-weight: 600; }}
QLabel#stateItemEn {{ color: {MUTED}; font-family: Consolas, monospace; font-size: 10px; }}
QPushButton#example {{
    background: {CARD}; color: {TEXT}; border: 1px solid {LINE};
    border-radius: 14px; padding: 6px 12px; font-size: 12px;
}}
QPushButton#example:hover {{ border-color: {ACCENT2}; background: #182034; }}
QLineEdit {{
    background: {CARD}; border: 1px solid {LINE}; border-radius: 10px;
    padding: 8px 10px; font-size: 13px; selection-background-color: {ACCENT2};
}}
QLineEdit:focus {{ border-color: {ACCENT2}; }}

/* Токены: полоса состава запроса, дорожка заполнения окна и диаграмма расхода */
QFrame#contextCard {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 12px; }}
QFrame#contextCard QLabel {{ background: transparent; }}
QFrame#barTrack {{ background: {BLACK}; border: 1px solid {LINE}; border-radius: 5px; }}
QLabel#tokenLine {{ color: {MUTED}; font-family: Consolas, monospace; font-size: 11px; }}
QLabel#tokenBig {{ font-family: Consolas, monospace; font-size: 13px; font-weight: 600; }}
QFrame#chart {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px; }}
"""

# Части запроса и их цвета: одни и те же в полосе контекста, в легенде и в окне
# токенов — по цвету видно, что именно занимает окно модели.
# Постоянная часть запроса на диаграмме: приглушённее памяти, чтобы был виден
# именно её рост.
FIXED_PART = "#2f4a86"

PART_COLORS = {
    "инструкция": "#7a5cff",
    "профиль": PERSONA,
    "суммаризация": SUMMARY,
    "долговременная": FACTS,
    "задача": TASK,
    "память": ACCENT,
    "вопрос": OK,
    "схемы инструментов": WARN,
}

# Цвет строки «стратегия: …» под ответом — по стратегии, чтобы он совпадал с сегментом
# полосы. Строка про сжатие идёт отдельной и всегда цветом суммаризации: сжатие — не
# стратегия, а опция поверх любой из них.
STRATEGY_COLORS = {"facts": FACTS, "branches": BRANCH, "window": MUTED}

# Масштаб интерфейса: все размеры в QSS заданы в пикселях, поэтому Ctrl+колесо
# просто пересобирает таблицу стилей, умножая числа перед «px». Отдельной копии
# стилей под каждый масштаб не нужно.
MIN_SCALE, MAX_SCALE = 0.8, 2.0


def build_qss(scale: float = 1.0) -> str:
    if abs(scale - 1.0) < 0.01:
        return QSS
    return re.sub(r"(\d+)px", lambda m: f"{max(1, round(int(m.group(1)) * scale))}px", QSS)


class AskWorker(QThread):
    """Обращение к агенту в отдельном потоке: окно не должно замирать на вызове.

    Кроме результата поток отдаёт события по ходу работы (`progress`): план
    составлен, шаг выполнен, задача сменила этап. Агент про Qt по-прежнему не
    знает — он зовёт обычный колбэк, а сигнал из него делает уже этот класс.
    """

    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str, object)

    def __init__(
        self,
        agent: Agent,
        message: str,
        compare: bool = False,
        compare_with: str | None = None,
    ) -> None:
        super().__init__()
        self.agent = agent
        self.message = message
        self.compare = compare   # заодно теневой ответ «без сжатия» для сравнения
        self.compare_with = compare_with   # id профиля для сравнения «ответы для разных профилей»

    def run(self) -> None:
        try:
            self.done.emit(self.agent.ask(
                self.message, compare=self.compare, on_event=self.progress.emit,
                compare_with=self.compare_with,
            ))
        except AgentError as e:
            self.failed.emit(str(e))


class Composer(QTextEdit):
    """Поле ввода: Enter отправляет, Shift+Enter переносит строку."""

    submitted = Signal()

    def keyPressEvent(self, event) -> None:
        enter = event.key() in (Qt.Key_Return, Qt.Key_Enter)
        if enter and not event.modifiers() & Qt.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class Bubble(QFrame):
    """Пузырь сообщения. Роль задаёт оформление: свой, агента или ошибка."""

    def __init__(self, text: str, role: str, max_width: int = 660) -> None:
        super().__init__()
        self.max_width = max_width
        self.setObjectName(BUBBLE_ID[role])
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.setMaximumWidth(max_width)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 11, 16, 12)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.label)
        self._fit(text)

    def _fit(self, text: str) -> None:
        """Подобрать ширину под текст.

        QLabel с переносом отдаёт нарочито узкий sizeHint, и пузырь схлопывается
        в колонку в пару слов. Поэтому считаем ширину самой длинной строки сами и
        ставим её минимальной, а перенос включается уже на потолке MAX_WIDTH.
        """
        fm = self.label.fontMetrics()
        longest = max((fm.horizontalAdvance(line) for line in text.split("\n")), default=0)
        ideal = int(longest * 1.1) + 44  # запас на отступы и разницу шрифта QSS
        self.setMinimumWidth(max(90, min(self.max_width, ideal)))

    def set_text(self, text: str) -> None:
        self.label.setText(text)
        self._fit(text)

    def set_role(self, role: str) -> None:
        self.setObjectName(BUBBLE_ID[role])
        self.style().unpolish(self)
        self.style().polish(self)


class ChatBody(QWidget):
    """Тело ленты: свою высоту считает по фактической ширине, а не по догадке.

    `QScrollArea` растягивает вложенный виджет по его `sizeHint` и про
    `heightForWidth` не спрашивает. А `sizeHint` у метки с переносом посчитан по
    узкой догадке о ширине — то есть с запасом в несколько строк на каждую. Из-за
    этого лента получалась заметно выше содержимого: под последним сообщением
    оставалось пустое поле, и прокрутка «в конец» уезжала в него. Здесь `sizeHint`
    и есть высота при нынешней ширине.
    """

    def _height(self) -> int:
        layout = self.layout()
        width = self.width()
        if layout is None or not layout.hasHeightForWidth() or width <= 0:
            return -1
        return layout.heightForWidth(width)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        height = self._height()
        return QSize(hint.width(), height) if height > 0 else hint

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        height = self._height()
        return QSize(hint.width(), height) if height > 0 else hint

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.updateGeometry()   # ширина сменилась — высота считается заново


class ChatView(QScrollArea):
    """Лента диалога: пузыри и мета-строки, всегда прокрученная к последнему."""

    def __init__(self, bubble_max: int = 660) -> None:
        super().__init__()
        self.bubble_limit = bubble_max   # желаемый потолок (растёт с масштабом)
        self.bubble_max = bubble_max     # фактический, с учётом ширины ленты
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = ChatBody()
        self.lay = QVBoxLayout(body)
        self.lay.setContentsMargins(26, 22, 18, 22)
        self.lay.setSpacing(10)
        self.lay.addStretch(1)
        self.setWidget(body)
        # Прилипание к концу ленты: высота строк уточняется уже после вставки (метки
        # переносятся по словам), поэтому одного скачка к низу мало — досылаем его
        # каждый раз, когда диапазон прокрутки пересчитали. Как только пользователь
        # сам ушёл вверх читать, прилипание отпускаем и ленту под ним не дёргаем.
        self._stick = True
        self.verticalScrollBar().rangeChanged.connect(self._range_changed)
        self.verticalScrollBar().valueChanged.connect(self._value_changed)

    def add(self, widget: QWidget, align=Qt.AlignLeft) -> QWidget:
        """Добавить виджет строкой ленты, прижав его к левому или правому краю."""
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        if align == Qt.AlignRight:
            row_lay.addStretch(1)
            row_lay.addWidget(widget)
        else:
            row_lay.addWidget(widget)
            row_lay.addStretch(1)
        return self.add_row(row, widget)

    def add_row(self, row: QWidget, result: QWidget | None = None) -> QWidget:
        """Добавить готовую строку во всю ширину ленты (без прижатия к краю).

        Строке обязательно включаем heightForWidth. Внутри неё живут метки с
        переносом, и они умеют считать высоту по фактической ширине, но вертикальный
        layout спрашивает об этом саму строку: без этой политики он берёт её
        `sizeHint`, посчитанный по узкой догадке о ширине, и резервирует высоту с
        запасом. Тогда лента оказывается выше содержимого — внизу пустое поле, а
        прокрутка «в конец» уезжает в него.
        """
        policy = row.sizePolicy()
        policy.setVerticalPolicy(QSizePolicy.Minimum)
        policy.setHeightForWidth(True)
        row.setSizePolicy(policy)
        self.lay.insertWidget(self.lay.count() - 1, row)  # перед распоркой в конце
        self._stick = True          # новое сообщение — значит, смотрим на конец ленты
        QTimer.singleShot(0, self._to_bottom)
        return result if result is not None else row

    def add_bubble(self, text: str, role: str) -> Bubble:
        bubble = Bubble(text, role, self.bubble_max)
        return self.add(bubble, Qt.AlignRight if role == "user" else Qt.AlignLeft)

    def to_top(self) -> None:
        """Показать начало ленты (в окне памяти это список, а не живой диалог)."""
        self._stick = False
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(0))

    def add_system(self, text: str) -> None:
        label = QLabel(text)
        label.setObjectName("system")
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 6, 0, 6)
        row_lay.addWidget(label)   # во всю ширину: текст центрируется внутри метки
        self.add_row(row)

    def add_marker(self, text: str, name: str = "marker") -> None:
        """Метка в ленте: точка ветвления, граница общего начала или смена этапа."""
        label = QLabel(text)
        label.setObjectName(name)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 4, 0, 4)
        row_lay.addWidget(label)
        self.add_row(row)

    def clear(self) -> None:
        while self.lay.count() > 1:
            item = self.lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # setParent(None) убирает строку из ленты сразу: одного deleteLater
                # мало — до следующего прохода цикла событий старые пузыри живы.
                widget.setParent(None)
                widget.deleteLater()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.refit()

    def refit(self) -> None:
        """Подогнать ширину пузырей под ленту.

        Потолок растёт вместе с масштабом интерфейса, но пузырь не должен быть
        шире самой ленты — иначе на большом масштабе текст уезжает за край.
        """
        available = max(240, self.viewport().width() - 90)
        limit = min(self.bubble_limit, available)
        if limit == self.bubble_max:
            return
        self.bubble_max = limit
        for bubble in self.findChildren(Bubble):
            bubble.max_width = limit
            bubble.setMaximumWidth(limit)
            bubble._fit(bubble.label.text())

    def _to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _range_changed(self, _minimum: int, maximum: int) -> None:
        """Диапазон прокрутки пересчитали — если мы «внизу», остаёмся внизу."""
        if self._stick:
            self.verticalScrollBar().setValue(maximum)

    def _value_changed(self, value: int) -> None:
        """Пользователь крутит ленту: у нижнего края — прилипаем, выше — отпускаем."""
        bar = self.verticalScrollBar()
        self._stick = value >= bar.maximum() - max(8, bar.singleStep())


class RawDialog(QDialog):
    """Сырой обмен: тело запроса, собранное агентом, и ответ модели как есть."""

    def __init__(self, reply: AgentReply, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Сырой обмен · обращение #{reply.turn}")
        self.resize(780, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        note = QLabel("Окно отправило одну строку — вот что из неё собрал агент.")
        note.setObjectName("note")
        lay.addWidget(note)
        tabs = QTabWidget()
        for title, data in (("→ запрос", reply.request), ("← ответ модели", reply.response)):
            view = QPlainTextEdit(json.dumps(data, ensure_ascii=False, indent=2))
            view.setObjectName("raw")
            view.setReadOnly(True)
            view.setLineWrapMode(QPlainTextEdit.NoWrap)
            tabs.addTab(view, title)
        lay.addWidget(tabs)


class MemoryDialog(QDialog):
    """Вся память агента в одном окне: слои, история и границы внутри неё.

    Первая вкладка — переписка как диалог с границами: что свёрнуто в суммаризацию
    или заменено долговременной памятью, что ждёт очереди, что просто за окном и что
    уходит в модель дословно. Вторая — слои: что лежит в каждом прямо сейчас, кто
    это туда положил и во что он обходится. Третья — суммаризация. Четвёртая — те же
    сообщения, записи, задачи и ветки строками таблиц базы: видно, что слои и правда
    хранятся отдельно друг от друга.
    """

    def __init__(self, agent: Agent, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Память и история агента «{agent.profile.name}»")
        self.resize(700, 580)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)

        p = agent.passport()
        history = agent.transcript()
        weight = agent.tokens_state()
        summary = weight["summary"]
        long = weight["long"]
        # Блоков может быть сразу три: слои независимы, а сжатие — опция поверх
        # стратегии, поэтому суммаризация, долговременная память и задача уживаются
        # в одном запросе.
        blocks = []
        if weight["summary_active"]:
            blocks.append(f"суммаризация №{summary['version']} вместо {weight['folded_messages']} сообщ.: "
                          f"≈ {_num(weight['folded_tokens'])} → {_num(weight['summary_tokens'])} т.")
        if weight["long_active"]:
            blocks.append(f"долговременная №{long['version']}: {long['count']} зап. ≈ "
                          f"{_num(weight['long_tokens'])} т.")
        if weight["task_active"]:
            blocks.append(
                f"задача «{weight['task']['title']}» на паузе: закладка ≈ "
                f"{_num(weight['task_tokens'])} т."
                if weight["task"]["paused"] else
                f"задача «{weight['task']['title']}»: {weight['task']['size']} пункт(ов) ≈ "
                f"{_num(weight['task_tokens'])} т.")
        block = "".join(f" · {item}" for item in blocks)
        branch = f" · ветка «{weight['branch_name']}»" if weight["branch"] != MAIN_BRANCH else ""
        head = QLabel(
            f"{weight['strategy_label']}{' + суммаризация' if weight['summarize'] else ''}{branch} · "
            f"{len(history)} сообщ. ≈ {_num(weight['history_tokens'])} "
            f"токенов в истории · {p['memory_messages']} сообщ. ≈ {_num(weight['breakdown']['memory'])} токенов "
            f"уйдёт в модель как есть{block} · {p['history_file'] or 'история не ведётся'}"
            if history else "История пуста — агент ещё ничего не запомнил."
        )
        head.setObjectName("note")
        head.setWordWrap(True)
        lay.addWidget(head)

        chat = ChatView(bubble_max=480)
        # Границы. Что не новее границы суммаризации — свёрнуто; дальше — очередь на
        # суммаризацию, «заменено долговременной памятью» или просто «за окном»; хвост — окно.
        upto = summary["upto"] if weight["summary_active"] else 0
        folded_count = sum(1 for m in history if (m.get("id") or 0) <= upto) if upto else 0
        window_start = len(history) - p["memory_messages"]
        strategy = weight["strategy"]
        for number, m in enumerate(history):
            if number == 0 and folded_count:
                chat.add_system(
                    f"↓ первые {folded_count} сообщ. свёрнуты в суммаризацию №{summary['version']} "
                    f"(≈ {_num(weight['folded_tokens'])} т. → {_num(weight['summary_tokens'])} т.): "
                    f"в модель уходит она, а не они"
                )
            if number == folded_count and window_start > folded_count:
                waiting = window_start - folded_count
                if weight["summarize"]:
                    text = (f"↓ {waiting} сообщ. ждут суммаризации — она обновится, когда их наберётся "
                            f"{weight['summary_every']}; пока в модель они не уходят")
                elif strategy == "facts":
                    text = (f"↓ {waiting} сообщ. за окном памяти: в модель они не уходят, их заменяет "
                            f"долговременная память ({long['count']} зап. ≈ "
                            f"{_num(weight['long_tokens'])} т.)")
                else:
                    text = (f"↓ {waiting} сообщ. за окном памяти: хранятся, но в модель не уходят "
                            f"и денег больше не стоят — {weight['strategy_label'].lower()}")
                chat.add_system(text)
            if number == window_start and p["memory_messages"]:
                chat.add_system(
                    f"↓ последние {p['memory_messages']} сообщ. ≈ "
                    f"{_num(weight['breakdown']['memory'])} токенов уходят в модель как есть — "
                    f"это окно контекста"
                )
            chat.add_bubble(m["content"], "user" if m["role"] == "user" else "agent")
        chat.to_top()

        tabs = QTabWidget()
        tabs.addTab(chat, "Диалог")
        tabs.addTab(_layers_page(weight, agent.tasks()), "Слои")
        tabs.addTab(_summary_page(weight), "Суммаризация")
        tabs.addTab(_history_table(agent, history), "В базе")
        lay.addWidget(tabs)


class ContextBar(QFrame):
    """Во что обойдётся следующий запрос — видно ещё до того, как его отправили.

    Верхняя полоса показывает состав запроса в долях: инструкция агента, память,
    схемы инструментов. По ней сразу заметно то, что обычно упускают, — на коротком
    диалоге дороже всего стоят не слова пользователя, а описания инструментов.
    Нижняя дорожка — тот же запрос в масштабе окна модели.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("contextCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 11)
        lay.setSpacing(7)

        self.total = QLabel()
        self.total.setObjectName("tokenBig")
        lay.addWidget(self.total)

        self.stack = QWidget()
        self.stack.setFixedHeight(10)
        self.stack_lay = QHBoxLayout(self.stack)
        self.stack_lay.setContentsMargins(0, 0, 0, 0)
        self.stack_lay.setSpacing(2)
        lay.addWidget(self.stack)

        self.legend = QLabel()
        self.legend.setObjectName("tokenLine")
        self.legend.setWordWrap(True)
        lay.addWidget(self.legend)

        track = QFrame()
        track.setObjectName("barTrack")
        track.setFixedHeight(10)
        track_lay = QHBoxLayout(track)
        track_lay.setContentsMargins(2, 2, 2, 2)
        track_lay.setSpacing(0)
        self.filled = QFrame()
        self.filled.setMinimumWidth(2)
        self.filled.setStyleSheet(f"background: {ACCENT2}; border-radius: 2px;")
        self.rest = QWidget()
        track_lay.addWidget(self.filled)
        track_lay.addWidget(self.rest)
        self.track_lay = track_lay
        lay.addWidget(track)

        self.window_line = QLabel()
        self.window_line.setObjectName("tokenLine")
        self.window_line.setWordWrap(True)
        lay.addWidget(self.window_line)

        # Третье число — вся переписка на диске. Она обычно тяжелее контекста, и
        # разница между «хранится» и «уходит в модель» — это и есть цена памяти.
        self.history_line = QLabel()
        self.history_line.setObjectName("tokenLine")
        self.history_line.setWordWrap(True)
        lay.addWidget(self.history_line)

    def show_state(self, state: dict) -> None:
        """Перерисовать полосу по состоянию агента (`Agent.tokens_state`)."""
        total = max(1, state["context_tokens"])
        parts = [(name, value) for name, value in state["parts"] if value > 0]

        while self.stack_lay.count():
            widget = self.stack_lay.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)      # иначе старые сегменты живут до следующего цикла
                widget.deleteLater()
        for name, value in parts:
            segment = QFrame()
            segment.setMinimumWidth(3)
            segment.setStyleSheet(f"background: {PART_COLORS.get(name, ACCENT)}; border-radius: 4px;")
            segment.setToolTip(f"{name}: {_num(value)} токенов")
            self.stack_lay.addWidget(segment, max(1, round(value / total * 1000)))

        self.total.setText(f"{_num(total)} токенов в следующем запросе")
        self.legend.setText(" · ".join(
            f'<span style="color: {PART_COLORS.get(name, ACCENT)}">■</span> {name} {_num(value)}'
            for name, value in parts
        ) or "пока пусто")

        fill = state["fill"]
        self.track_lay.setStretch(0, max(1, round(fill * 1000)))
        self.track_lay.setStretch(1, max(1, 1000 - round(fill * 1000)))
        self.window_line.setText(
            f"окно модели {_num(state['limit'])} · занято {fill * 100:.2f}% · "
            f"запас под ответ {_num(state['reserve'])}"
        )
        # Строка собирается из частей: слои и сжатие работают независимо, и в запрос
        # может уйти сразу и суммаризация, и долговременная память, и задача, и окно.
        strategy = state["strategy"]
        parts = [f"вся история: {state['history_messages']} сообщ. ≈ {_num(state['history_tokens'])} т."]
        if strategy == "branches":
            parts.append(f"ветка «{state['branch_name']}»")
        sent = []
        # Профиль пользователя идёт первым: он определяет не содержание ответа, а
        # его форму, и платится в каждом запросе одинаково — в отличие от слоёв.
        if state["persona_active"]:
            sent.append(f"профиль «{state['persona']['name']}» ≈ {_num(state['persona_tokens'])} т.")
        if state["summary_active"]:
            sent.append(f"суммаризация №{state['summary']['version']} вместо {state['folded_messages']} "
                        f"сообщ. (≈ {_num(state['folded_tokens'])} → {_num(state['summary_tokens'])} т.)")
        if state["long_active"]:
            sent.append(f"долговременная №{state['long']['version']} ({state['long']['count']} зап. ≈ "
                        f"{_num(state['long_tokens'])} т.)")
        if state["task_active"]:
            sent.append(
                f"закладка отложенной задачи «{state['task']['title']}» "
                f"(≈ {_num(state['task_tokens'])} т.)"
                if state["task"]["paused"] else
                f"задача «{state['task']['title']}» ({state['task']['size']} пункт(ов) ≈ "
                f"{_num(state['task_tokens'])} т.)")
        sent.append(f"{state['window_messages']} сообщ. как есть")
        parts.append("в модель: " + " + ".join(sent))
        if state["summarize"]:
            if not state["summary_active"]:
                parts.append(f"суммаризации пока нет: за окном {state['pending_messages']} сообщ. "
                             f"из {state['summary_every']}")
            elif state["pending_messages"]:
                parts.append(f"ждут суммаризации: {state['pending_messages']} из {state['summary_every']}")
        elif state["dropped_messages"]:
            why = {"facts": " — их заменяет долговременная память",
                   "window": " — отброшены (скользящее окно)"}
            parts.append(f"за окном {state['dropped_messages']} сообщ. ≈ "
                         f"{_num(state['dropped_tokens'])} т." + why.get(strategy, ""))
        if strategy == "facts" and not state["long_active"]:
            parts.append("долговременная память пока пуста — появится после первого ответа")
        if state["working"] and not state["task_active"]:
            parts.append("задачи нет — рабочая память пуста")
        if state["task_active"] and state["task"]["paused"]:
            parts.append(f"задача отложена на этапе «{state['task']['state_label']}»: "
                         f"{state['task']['size']} пункт(ов) карточки ждут в базе, "
                         f"ждём: {state['task']['expect_line']}")
        if state["route_pending"]:
            parts.append(f"ждут маршрутизации: {state['route_pending']} сообщ.")
        self.history_line.setText(" · ".join(parts))
        self.setToolTip(
            "Считается до отправки: инструкция агента, блоки слоёв памяти (долговременная, задача,\n"
            "суммаризация), окно памяти и схемы инструментов уже известны, значит известен и вес\n"
            "следующего запроса. История хранится целиком, но платим мы только за то, что\n"
            "попадает в окно контекста."
        )


class UsageChart(QFrame):
    """Как растёт расход: столбик на обращение и линия накопленной стоимости.

    Столбики — токены запроса и ответа, линия — сколько потрачено суммарно. Именно
    здесь видно главное свойство диалога с памятью: ответы остаются примерно
    одинаковыми, а запрос дорожает с каждым обменом, потому что тащит за собой всю
    предыдущую переписку.
    """

    def __init__(self, rows: list[dict]) -> None:
        super().__init__()
        self.setObjectName("chart")
        self.rows = rows
        self.setMinimumHeight(240)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(QFont("Consolas", 7))

        if not self.rows:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignCenter,
                             "Расхода пока нет — задайте агенту вопрос.")
            painter.end()
            return

        left, right, top, bottom = 62, 66, 16, 26
        width = max(1, self.width() - left - right)
        height = max(1, self.height() - top - bottom)
        # Столбик — это контекст запроса плюс ответ. Контекст разделён на части:
        # постоянная (инструкция и схемы инструментов платятся в каждом обращении
        # одинаково), блоки слоёв — суммаризация, долговременная память и задача, — и
        # память как есть: та, что растёт, пока её не остановит окно или стратегия.
        peak = max(row["context_tokens"] + row["completion_tokens"] for row in self.rows) or 1
        spent, running = [], 0.0
        for row in self.rows:
            running += row["cost_usd"] or 0.0
            spent.append(running)
        money_peak = spent[-1] or 1e-9

        painter.setPen(QPen(QColor(LINE), 1))
        painter.drawLine(left, top + height, left + width, top + height)

        step = width / len(self.rows)
        bar = max(3.0, min(26.0, step * 0.6))
        for number, row in enumerate(self.rows):
            centre = left + step * (number + 0.5)
            summary = min(row.get("summary_tokens") or 0, row["context_tokens"])
            # У строк дня 10 долговременная память лежит в колонке facts_tokens: слой
            # тогда назывался блоком фактов, и историю расхода терять из-за этого незачем.
            long = min(row.get("long_tokens") or row.get("facts_tokens") or 0,
                       row["context_tokens"] - summary)
            task = min(row.get("task_tokens") or 0, row["context_tokens"] - summary - long)
            who = min(row.get("persona_tokens") or 0,
                      row["context_tokens"] - summary - long - task)
            memory = min(row["memory_tokens"],
                         row["context_tokens"] - summary - long - task - who)
            blocks = (
                (row["context_tokens"] - memory - summary - long - task - who, FIXED_PART),  # инструкция, схемы, вопрос
                (who, PART_COLORS["профиль"]),               # профиль пользователя: платится всегда
                (summary, PART_COLORS["суммаризация"]),      # сжатое начало разговора
                (long, PART_COLORS["долговременная"]),       # профиль, решения, знания
                (task, PART_COLORS["задача"]),               # рабочая память
                (memory, PART_COLORS["память"]),             # то, что растёт с диалогом
                (row["completion_tokens"], OK),              # ответ модели
            )
            base = top + height
            for value, color in blocks:
                block = value / peak * height
                painter.fillRect(
                    int(centre - bar / 2), int(base - block), int(bar), int(block), QColor(color)
                )
                base -= block

        painter.setPen(QPen(QColor(WARN), 2))
        previous = None
        for number, value in enumerate(spent):
            point = (left + step * (number + 0.5), top + height - value / money_peak * height)
            if previous is not None:
                painter.drawLine(int(previous[0]), int(previous[1]), int(point[0]), int(point[1]))
            previous = point

        painter.setPen(QColor(MUTED))
        painter.drawText(4, top + 8, f"{_num(peak)} т.")
        painter.drawText(4, top + height, "0")
        painter.drawText(self.width() - right + 6, top + 8, f"${money_peak:.4f}")
        painter.drawText(left, self.height() - 8, "обращение 1")
        painter.drawText(self.width() - right - 34, self.height() - 8, f"#{self.rows[-1]['turn']}")
        painter.end()


class TokensDialog(QDialog):
    """Токены и стоимость: сколько уходит в модель, из чего это состоит и как растёт.

    Первая вкладка отвечает на вопрос «как дорожает разговор» — диаграмма и та же
    таблица числами, строка на обращение. Вторая показывает состав текущего
    контекста, разницу между историей на диске и тем, что реально уходит в модель,
    и прогноз: на сколько обменов хватит окна и во что они обойдутся. Третья —
    сравнение стратегий: сводная таблица по всем агентам окна, чтобы один и тот же
    сценарий, прогнанный с разными стратегиями, лёг рядом числами.
    """

    def __init__(self, agent: Agent, parent: QWidget, agents: list[Agent] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Токены и стоимость · агент «{agent.profile.name}»")
        self.resize(800, 640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        state = agent.tokens_state()
        rows = agent.usage_log()
        spent = state["spent"]
        calibration = state["calibration"]

        head = QLabel(
            f"{config.model_label(state['model'])} · окно {_num(state['limit'])} токенов · "
            f"потолок ответа {_num(state['max_output'])} · стратегия: {state['strategy_label'].lower()} · "
            f"сжатие: {'суммаризация' if state['summarize'] else 'выключено'} · "
            f"потрачено за всё время {_num(spent['total_tokens'])} токенов ({_money(spent['cost_usd'])}) "
            f"за {spent['turns']} обращени(й)"
        )
        head.setObjectName("note")
        head.setWordWrap(True)
        lay.addWidget(head)

        tabs = QTabWidget()
        tabs.addTab(self._growth_tab(agent.id, rows), "Рост по обращениям")
        tabs.addTab(self._context_tab(agent, state, rows, calibration), "Состав и прогноз")
        tabs.addTab(self._strategies_tab(agents or [agent]), "Стратегии")
        lay.addWidget(tabs)

    def _growth_tab(self, agent_id: str, rows: list[dict]) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(10)

        lay.addWidget(UsageChart(rows))
        legend = QLabel(
            f'<span style="color: {FIXED_PART}">■</span> постоянная часть запроса '
            f'(инструкция и схемы) · '
            f'<span style="color: {PART_COLORS["суммаризация"]}">■</span> суммаризация · '
            f'<span style="color: {PART_COLORS["долговременная"]}">■</span> долговременная · '
            f'<span style="color: {PART_COLORS["задача"]}">■</span> задача · '
            f'<span style="color: {PART_COLORS["память"]}">■</span> память как есть · '
            f'<span style="color: {OK}">■</span> ответ модели · '
            f'<span style="color: {WARN}">—</span> накопленная стоимость'
        )
        legend.setObjectName("tokenLine")
        legend.setWordWrap(True)
        lay.addWidget(legend)
        lay.addWidget(_usage_table(agent_id, rows), 1)
        return page

    def _strategies_tab(self, agents: list[Agent]) -> QWidget:
        """Сводка по агентам: стратегия, обращения, средний запрос, всего токенов, стоимость."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(10)
        note = QLabel(
            "Один и тот же сценарий на нескольких агентах с разными стратегиями — и расход ложится рядом. "
            "Качество и стабильность ответов числом не измерить: их видно по контрольным вопросам и по "
            "кнопке «Сравнить», которая кладёт рядом ответ с полной историей."
        )
        note.setObjectName("note")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addWidget(_strategies_table(agents), 1)
        return page

    def _context_tab(self, agent: Agent, state: dict, rows: list[dict], calibration: dict) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 10, 10, 0)
        lay.setSpacing(12)

        lay.addWidget(_section("ЧТО УЙДЁТ В МОДЕЛЬ СЛЕДУЮЩИМ ЗАПРОСОМ"))
        bar = ContextBar()
        bar.show_state(state)
        lay.addWidget(bar)

        lay.addWidget(_section("ИСТОРИЯ ЦЕЛИКОМ И ОКНО КОНТЕКСТА"))
        replaced = ""
        if state["summary_active"]:
            replaced += (f", а вместо {state['folded_messages']} сообщ. до него — суммаризация "
                         f"(≈{_num(state['summary_tokens'])} токенов)")
        if state["long_active"]:
            replaced += (f", а сверх того — долговременная память по всей переписке "
                         f"({state['long']['count']} зап. ≈{_num(state['long_tokens'])} токенов)")
        if state["task_active"]:
            replaced += (f" и карточка текущей задачи ({state['task']['size']} пункт(ов) "
                         f"≈{_num(state['task_tokens'])} токенов)")
        history = QLabel(
            f"В базе лежит {state['history_messages']} сообщ. — это ≈{_num(state['history_tokens'])} "
            f"токенов. В модель как есть уходит только окно памяти: {state['window_messages']} сообщ. "
            f"(≈{_num(state['breakdown']['memory'])} токенов){replaced}. Разница и есть смысл "
            "многослойной памяти: разговор хранится целиком, а платим мы только за хвост и за "
            "короткие блоки слоёв."
        )
        history.setObjectName("subtitle")
        history.setWordWrap(True)
        lay.addWidget(history)

        # Персонализация платится иначе, чем память: её вес не растёт с разговором,
        # зато взимается в каждом обращении — это стоит видеть отдельной строкой.
        lay.addWidget(_section("ПЕРСОНАЛИЗАЦИЯ: ЦЕНА И ПРОВЕРКА"))
        persona_note = QLabel(_persona_text(state, rows))
        persona_note.setObjectName("subtitle")
        persona_note.setWordWrap(True)
        lay.addWidget(persona_note)

        lay.addWidget(_section("СТРАТЕГИЯ КОНТЕКСТА: ДО И ПОСЛЕ"))
        strategy_note = QLabel(_strategy_text(state, rows))
        strategy_note.setObjectName("subtitle")
        strategy_note.setWordWrap(True)
        lay.addWidget(strategy_note)

        # Сжатие — опция поверх стратегии, поэтому и раздел у него свой: видно, что
        # стратегия и суммаризация экономят по-разному и считаются отдельно.
        lay.addWidget(_section("СЖАТИЕ ИСТОРИИ: ДО И ПОСЛЕ"))
        compression_note = QLabel(_compression_text(state, rows))
        compression_note.setObjectName("subtitle")
        compression_note.setWordWrap(True)
        lay.addWidget(compression_note)

        lay.addWidget(_section("ТОЧНОСТЬ СЧЁТЧИКА"))
        accuracy = QLabel(_accuracy_text(calibration))
        accuracy.setObjectName("subtitle")
        accuracy.setWordWrap(True)
        lay.addWidget(accuracy)

        lay.addWidget(_section("КОГДА УПРЁМСЯ В ЛИМИТ"))
        forecast = QLabel(_forecast_text(state, rows))
        forecast.setObjectName("subtitle")
        forecast.setWordWrap(True)
        lay.addWidget(forecast)

        lay.addStretch(1)
        page.setWidget(body)
        return page


class AgentItem(QFrame):
    """Карточка агента в списке: клик выбирает, крестик удаляет."""

    chosen = Signal()
    removed = Signal()

    def __init__(self, agent: Agent, active: bool, deletable: bool) -> None:
        super().__init__()
        self.setObjectName("agentItemActive" if active else "agentItem")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setToolTip(
            f"{agent.profile.name} — {agent.profile.role}\n"
            f"{config.model_label(agent.model)} · обращений: {agent.turns} · "
            f"в памяти: {len(agent.memory)} сообщ. · в истории: {agent.history_size} сообщ."
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 8, 7)
        lay.setSpacing(7)

        name = QLabel(agent.profile.name)
        name.setObjectName("agentItemName")
        lay.addWidget(name)

        counter = QLabel(f"{agent.turns} обр. · {len(agent.memory)} в памяти")
        counter.setObjectName("agentItemSub")
        lay.addWidget(counter)

        if deletable:
            remove = QPushButton("✕")
            remove.setObjectName("link")
            remove.setCursor(Qt.PointingHandCursor)
            remove.setToolTip("Удалить агента вместе с его памятью")
            remove.clicked.connect(self.removed.emit)
            lay.addWidget(remove, 0, Qt.AlignTop)

    def mousePressEvent(self, event) -> None:
        self.chosen.emit()
        super().mousePressEvent(event)


class BranchItem(QFrame):
    """Ветка в полосе веток: клик переключает, крестик удаляет (основную удалить нельзя)."""

    chosen = Signal()
    removed = Signal()

    def __init__(self, branch: dict) -> None:
        super().__init__()
        self.setObjectName("branchItemActive" if branch["active"] else "branchItem")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        origin = f"от точки «{branch['origin']}», общих сообщ.: {branch['shared']}" if branch["origin"] else \
            "основная линия разговора"
        self.setToolTip(f"Ветка «{branch['name']}» · {origin} · сообщений: {branch['messages']}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 6, 5)
        lay.setSpacing(6)
        name = QLabel(branch["name"])
        name.setObjectName("branchItemName")
        lay.addWidget(name)
        counter = QLabel(f"{branch['messages']} сообщ.")
        counter.setObjectName("branchItemSub")
        lay.addWidget(counter)
        if branch["id"] != MAIN_BRANCH:
            remove = QPushButton("✕")
            remove.setObjectName("link")
            remove.setCursor(Qt.PointingHandCursor)
            remove.setToolTip("Удалить ветку вместе с её перепиской")
            remove.clicked.connect(self.removed.emit)
            lay.addWidget(remove, 0, Qt.AlignTop)

    def mousePressEvent(self, event) -> None:
        self.chosen.emit()
        super().mousePressEvent(event)


class ElidedLabel(QLabel):
    """Однострочная подпись, которая всегда вписана в свою фактическую ширину.

    Обычный QLabel в узкой полосе ведёт себя плохо: длинный текст либо распирает
    строку и выдавливает соседей, либо молча обрезается по краю на полуслове. Здесь
    полный текст хранится отдельно, а показывается ровно столько, сколько влезло, с
    многоточием в конце — и пересчитывается сам, когда окно меняет размер.
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self.setObjectName(name)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._full = ""

    def setFullText(self, text: str) -> None:
        self._full = text
        self._apply()

    def fullText(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        # До первой раскладки ширина нулевая — берём разумную оценку, настоящую
        # применит resizeEvent, как только полоса разложится.
        room = self.width() if self.width() > 40 else 240
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, room))


class StateItem(QFrame):
    """Этап задачи в полосе автомата — индикатор, а не кнопка.

    Кликать здесь нечего и не нужно: этапы агент проходит сам. Код двигает автомат
    по факту работы (появился выполненный шаг — значит идёт выполнение), а
    маршрутизатор — по смыслу разговора. Кликабельные этапы у этой полосы были и
    оказались вредны: пользователь решал, что переключать их — его работа, и гонял
    задачу между этапами руками.
    """

    def __init__(self, state: dict, active: bool, allowed: bool, paused: bool = False) -> None:
        super().__init__()
        self.setObjectName(("stateItemPaused" if paused else "stateItemActive") if active else
                           ("stateItem" if allowed and not paused else "stateItemLocked"))
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setToolTip(
            f"{state['en']} — {state['what']}."
            + ("\nЗадача остановлена здесь: пока стоит пауза, автомат заморожен и "
               "любой переход отклоняется." if active and paused else
               "\nЗадача здесь прямо сейчас." if active else
               "\nЗадача на паузе: переходы заморожены, пока работу не продолжат." if paused else
               ("\nСледующий возможный этап — агент перейдёт сюда сам." if allowed else
                "\nСюда из текущего этапа перейти нельзя: этапы нельзя перепрыгивать."))
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(11, 5, 11, 6)
        lay.setSpacing(0)
        name = QLabel(state["label"])
        name.setObjectName("stateItemName")
        lay.addWidget(name)
        english = QLabel(state["en"])
        english.setObjectName("stateItemEn")
        lay.addWidget(english)


class ProfileDialog(QDialog):
    """Паспорт агента: имя, подпись роли, инструкция и модель.

    Одно окно на два случая — завести нового агента и поправить профиль
    существующего. Поля те же, меняются заголовок и кнопка; при правке заготовки
    и выбор модели не показываются (модель меняется в боковой панели, а заготовка
    затёрла бы то, что уже написано).
    """

    def __init__(self, parent: QWidget, agent: Agent | None = None) -> None:
        super().__init__(parent)
        self.agent = agent
        self.setWindowTitle("Новый агент" if agent is None else "Профиль агента")
        self.resize(560, 500)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        note = QLabel(
            "У каждого агента своя роль, своя память и свои настройки."
            if agent is None else
            "Имя, подпись и инструкция. Память и история агента останутся при нём."
        )
        note.setObjectName("note")
        note.setWordWrap(True)
        lay.addWidget(note)

        self.preset = QComboBox()
        if agent is None:
            lay.addWidget(_section("ЗАГОТОВКА ПРОФИЛЯ"))
            for profile in config.PROFILE_PRESETS:
                title = profile.get("title") or f"{profile['name']} — {profile['role']}"
                self.preset.addItem(title, profile)
            self.preset.currentIndexChanged.connect(self._fill_from_preset)
            lay.addWidget(self.preset)

        lay.addWidget(_section("ИМЯ"))
        self.name = QLineEdit()
        self.name.setMaxLength(40)
        self.name.setPlaceholderText("Как зовут агента — видно на вкладке")
        lay.addWidget(self.name)

        lay.addWidget(_section("ПОДПИСЬ: ЧЕМ ЗАНИМАЕТСЯ"))
        self.role = QLineEdit()
        self.role.setMaxLength(60)
        self.role.setPlaceholderText("Короткая подпись под именем, например «лидер автоботов»")
        lay.addWidget(self.role)

        lay.addWidget(_section("ИНСТРУКЦИЯ (ХАРАКТЕР И ПРАВИЛА)"))
        self.instructions = QPlainTextEdit()
        self.instructions.setPlaceholderText("Пусто — возьмётся инструкция агента по умолчанию")
        lay.addWidget(self.instructions, 1)

        self.model = QComboBox()
        self.strategy = QComboBox()
        if agent is None:
            lay.addWidget(_section("МОДЕЛЬ"))
            for item in config.MODELS:
                self.model.addItem(item["label"], item["code"])
            self.model.setCurrentIndex(max(0, self.model.findData(config.DEFAULT_MODEL)))
            lay.addWidget(self.model)
            # Стратегия контекста задаётся сразу: для сравнения стратегий агентов заводят
            # по одному на стратегию, и кликать потом в панели каждому — лишний шаг.
            lay.addWidget(_section("СТРАТЕГИЯ КОНТЕКСТА"))
            for item in config.STRATEGIES:
                # В выпадающем списке места больше, чем в панели: английское название
                # из задания помещается в ту же строку.
                self.strategy.addItem(config.strategy_title(item["code"]), item["code"])
                self.strategy.setItemData(self.strategy.count() - 1,
                                          _strategy_hint(item["code"], config.AGENT_MEMORY_TURNS),
                                          Qt.ToolTipRole)
            self.strategy.setCurrentIndex(max(0, self.strategy.findData(config.AGENT_STRATEGY)))
            lay.addWidget(self.strategy)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        apply_btn = QPushButton("Создать" if agent is None else "Сохранить")
        apply_btn.setObjectName("send")
        apply_btn.setFixedHeight(40)
        apply_btn.setCursor(Qt.PointingHandCursor)
        apply_btn.clicked.connect(self.accept)
        cancel = _ghost("Отмена")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        buttons.addWidget(apply_btn, 1)
        lay.addLayout(buttons)

        if agent is None:
            self._fill_from_preset()
        else:
            self.name.setText(agent.profile.name)
            self.role.setText(agent.profile.role)
            self.instructions.setPlainText(agent.profile.instructions)
        self.name.setFocus()

    def _fill_from_preset(self) -> None:
        """Подставить заготовку. «Свой профиль» — пустые поля, пишем сами."""
        profile = self.preset.currentData()
        self.name.setText(profile["name"])
        self.role.setText(profile["role"])
        self.instructions.setPlainText(profile["instructions"])

    def values(self) -> dict:
        return {
            "name": self.name.text().strip() or "Агент",
            "role": self.role.text().strip() or "агент со своей памятью",
            "instructions": self.instructions.toPlainText().strip(),
            "model": self.model.currentData() or config.DEFAULT_MODEL,
            "strategy": self.strategy.currentData() or config.AGENT_STRATEGY,
        }


class PersonaDialog(QDialog):
    """Профиль пользователя: кто вы и в какой форме агент должен отвечать.

    Три группы предпочтений — ровно те, что перечислены в задании: стиль, формат
    (вместе с длиной) и ограничения. Готовые ограничения отмечаются флажками и
    проверяются кодом после ответа; своё ограничение можно вписать словами — оно
    уйдёт в инструкцию просьбой, и в подсказке так и сказано.

    Одно окно на два случая, как у паспорта агента: завести профиль и поправить
    существующий. При создании показываются заготовки, при правке — нет: они
    затёрли бы то, что уже выбрано.
    """

    def __init__(self, parent: QWidget, current: dict | None = None) -> None:
        super().__init__(parent)
        self.current = current
        self.setWindowTitle("Новый профиль пользователя" if current is None else "Профиль пользователя")
        self.resize(560, 640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        note = QLabel(
            "Профиль уходит в каждый запрос отдельным блоком: агент подстраивает под него "
            "стиль, формат и длину ответа. Длина, формат и отмеченные ограничения после "
            "ответа проверяются — расхождение видно под ответом."
        )
        note.setObjectName("note")
        note.setWordWrap(True)
        lay.addWidget(note)

        self.preset = QComboBox()
        if current is None:
            lay.addWidget(_section("ЗАГОТОВКА"))
            for item in config.PERSONA_PRESETS:
                self.preset.addItem(item["name"], item)
            self.preset.currentIndexChanged.connect(self._fill_from_preset)
            lay.addWidget(self.preset)

        lay.addWidget(_section("НАЗВАНИЕ ПРОФИЛЯ"))
        self.name = QLineEdit()
        self.name.setMaxLength(40)
        self.name.setPlaceholderText("Как назвать этот профиль — видно в панели")
        lay.addWidget(self.name)

        lay.addWidget(_section("КТО ВЫ"))
        self.about = QPlainTextEdit()
        self.about.setPlaceholderText(
            "Чем занимаетесь и что агенту стоит держать в голове: «не программист, снимаю видео»"
        )
        self.about.setFixedHeight(64)
        lay.addWidget(self.about)

        # Стиль, формат и длина — выбором из реестров: значение, которого нет в
        # реестре, нельзя ни объяснить модели, ни проверить в ответе.
        self.style = QComboBox()
        self.format = QComboBox()
        self.length = QComboBox()
        for caption, box, registry in (
            ("СТИЛЬ ОБЩЕНИЯ", self.style, config.PERSONA_STYLES),
            ("ФОРМАТ ОТВЕТА", self.format, config.PERSONA_FORMATS),
            ("ДЛИНА ОТВЕТА", self.length, config.PERSONA_LENGTHS),
        ):
            lay.addWidget(_section(caption))
            for item in registry:
                box.addItem(item["label"], item["code"])
                box.setItemData(box.count() - 1, item["rule"], Qt.ToolTipRole)
            lay.addWidget(box)

        lay.addWidget(_section("ОГРАНИЧЕНИЯ: ЧЕГО НЕ ДЕЛАТЬ"))
        self.limit_boxes: dict[str, QCheckBox] = {}
        for item in config.PERSONA_LIMITS:
            box = QCheckBox(item["label"])
            box.setToolTip(f"{item['rule']}\n\nПроверяется после ответа: {item['what']}.")
            self.limit_boxes[item["code"]] = box
            lay.addWidget(box)
        self.own_limits = QPlainTextEdit()
        self.own_limits.setPlaceholderText("Своё ограничение, по строке — уйдёт просьбой в инструкцию")
        self.own_limits.setToolTip(
            "Эти строки агент получит вместе с профилем, но проверить их код не может:\n"
            "в отличие от флажков выше, они остаются просьбой."
        )
        self.own_limits.setFixedHeight(56)
        lay.addWidget(self.own_limits)

        lay.addWidget(_section("ПРЕДПОЧТЕНИЯ: КЛЮЧ: ЗНАЧЕНИЕ, ПО СТРОКЕ"))
        self.prefs = QPlainTextEdit()
        self.prefs.setPlaceholderText("примеры: из съёмок и монтажа\nединицы: в рублях")
        self.prefs.setToolTip(
            "То, чего нет в списках выше. Сюда же агент дописывает предпочтения,\n"
            "замеченные в разговоре, — источник каждого видно в окне «Память и история»."
        )
        self.prefs.setFixedHeight(72)
        lay.addWidget(self.prefs, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        # Удаление живёт здесь же, где правка: заводить ради него отдельную кнопку в
        # панели незачем — профиль правят гораздо чаще, чем удаляют.
        self.deleted = False
        if current is not None:
            remove = _ghost("Удалить")
            remove.setToolTip(
                "Удалить профиль насовсем. Переписка и память не пострадают: агенты,\n"
                "которые им пользовались, останутся без профиля. Удалите все — при\n"
                "следующем запуске заготовки появятся снова."
            )
            remove.clicked.connect(self._remove)
            buttons.addWidget(remove)
        apply_btn = QPushButton("Создать" if current is None else "Сохранить")
        apply_btn.setObjectName("send")
        apply_btn.setFixedHeight(40)
        apply_btn.setCursor(Qt.PointingHandCursor)
        apply_btn.clicked.connect(self.accept)
        cancel = _ghost("Отмена")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        buttons.addWidget(apply_btn, 1)
        lay.addLayout(buttons)

        if current is None:
            self._fill_from_preset()
        else:
            self._fill(current)
        self.name.setFocus()

    def _remove(self) -> None:
        """Пометить профиль на удаление и закрыть окно — удаляет его агент."""
        self.deleted = True
        self.accept()

    def _fill_from_preset(self) -> None:
        """Подставить заготовку профиля — дальше её правят руками."""
        item = self.preset.currentData() or {}
        self._fill({
            "name": item.get("name", ""),
            "about": item.get("about", ""),
            "style": item.get("style", config.PERSONA_STYLE),
            "format": item.get("format", config.PERSONA_FORMAT),
            "length": item.get("length", config.PERSONA_LENGTH),
            "limits": item.get("limits") or [],
            "prefs": [{"key": key, "value": value} for key, value in (item.get("prefs") or {}).items()],
        })

    def _fill(self, values: dict) -> None:
        """Разложить профиль по полям окна (и свои ограничения — отдельно от флажков)."""
        self.name.setText(values.get("name", ""))
        self.about.setPlainText(values.get("about", ""))
        for box, code in ((self.style, values.get("style")), (self.format, values.get("format")),
                          (self.length, values.get("length"))):
            box.setCurrentIndex(max(0, box.findData(code)))
        limits = list(values.get("limits") or [])
        for code, box in self.limit_boxes.items():
            box.setChecked(code in limits)
        self.own_limits.setPlainText("\n".join(item for item in limits if item not in self.limit_boxes))
        self.prefs.setPlainText("\n".join(
            f"{item.get('key')}: {item.get('value')}" for item in values.get("prefs") or []
        ))

    def values(self) -> dict:
        """Профиль в том виде, в каком его примет агент (проверит он же)."""
        limits = [code for code, box in self.limit_boxes.items() if box.isChecked()]
        limits += [line.strip() for line in self.own_limits.toPlainText().splitlines() if line.strip()]
        prefs = {}
        for line in self.prefs.toPlainText().splitlines():
            key, _, value = line.partition(":")
            if key.strip() and value.strip():
                prefs[key.strip()] = value.strip()
        return {
            "name": self.name.text().strip() or "Профиль",
            "about": self.about.toPlainText().strip(),
            "style": self.style.currentData(),
            "format": self.format.currentData(),
            "length": self.length.currentData(),
            "limits": limits,
            "prefs": prefs,
        }


class NameDialog(QDialog):
    """Имя для новой ветки и место, от которого её вести.

    Заголовок, пояснение и, если передан список точек, выбор среди них. Первый пункт —
    «текущее место»: точка ветвления встанет здесь сама.
    """

    def __init__(
        self,
        parent: QWidget,
        title: str,
        note: str,
        caption: str,
        default: str,
        placeholder: str = "",
        choices: list[tuple[str, object]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(480, 260)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        text = QLabel(note)
        text.setObjectName("note")
        text.setWordWrap(True)
        lay.addWidget(text)

        self.choice = QComboBox()
        if choices:
            lay.addWidget(_section("ОТ КАКОЙ ТОЧКИ"))
            for label, data in choices:
                self.choice.addItem(label, data)
            lay.addWidget(self.choice)

        lay.addWidget(_section(caption))
        self.name = QLineEdit(default)
        self.name.setMaxLength(40)
        self.name.setPlaceholderText(placeholder)
        self.name.selectAll()
        lay.addWidget(self.name)
        lay.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        ok = QPushButton("Создать")
        ok.setObjectName("send")
        ok.setFixedHeight(40)
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self.accept)
        cancel = _ghost("Отмена")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        buttons.addWidget(ok, 1)
        lay.addLayout(buttons)
        self.name.setFocus()

    def value(self) -> str:
        return self.name.text().strip()

    def chosen(self):
        return self.choice.currentData()


class ToolsDialog(QDialog):
    """Чем агент умеет действовать: список инструментов из его паспорта."""

    def __init__(self, agent: Agent, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Инструменты агента")
        self.resize(600, 520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)

        passport = agent.passport()
        head = QLabel(
            f"{len(passport['tools'])} инструментов · рабочая папка: {passport['workspace']}"
            if passport["tools_enabled"]
            else "Инструменты сейчас выключены — агент может только отвечать текстом."
        )
        head.setObjectName("note")
        head.setWordWrap(True)
        lay.addWidget(head)

        area = QScrollArea()
        area.setWidgetResizable(True)
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 8, 8, 0)
        body_lay.setSpacing(10)
        for tool in passport["tools"]:
            name = QLabel(tool["name"])
            name.setObjectName("stepHead")
            description = QLabel(tool["description"])
            description.setObjectName("agentRole")
            description.setWordWrap(True)
            body_lay.addWidget(name)
            body_lay.addWidget(description)
        body_lay.addStretch(1)
        area.setWidget(body)
        lay.addWidget(area)


class AgentWindow(QMainWindow):
    """Окно диалога: слева паспорт и настройки агента, справа лента и ввод."""

    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("AI Advent", "Agent")   # масштаб переживает перезапуск
        self.scale = float(self.settings.value("ui/scale", 1.0))
        # Агенты приезжают из хранилища вместе с памятью и настройками: окно их
        # не собирает и не знает, что и в каком виде лежит на диске.
        self.store = Store()
        # Профили поднимаются ДО агентов: агент при восстановлении ищет свой профиль
        # по ссылке, и на первом запуске его ещё нужно завести из заготовок.
        self.personas = load_personas(self.store)
        self.agents, self.agent = load_agents(self.store)
        self.busy = False
        self.worker: AskWorker | None = None
        self._pending: Bubble | None = None
        self._loading = False  # чтобы программная установка контролов не била в агента

        self.setWindowTitle(f"Агент «{self.agent.profile.name}» — AI Advent")
        self.resize(1060, 720)
        self.setMinimumSize(880, 600)

        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._build_sidebar())
        lay.addWidget(self._build_main(), 1)
        self.setCentralWidget(central)

        # Масштаб: Ctrl+колесо ловим фильтром на приложении (иначе событие
        # съедает лента), плюс привычные Ctrl+= / Ctrl+- / Ctrl+0.
        QApplication.instance().installEventFilter(self)
        QShortcut(QKeySequence.ZoomIn, self, activated=lambda: self._zoom(0.1))
        QShortcut(QKeySequence("Ctrl+="), self, activated=lambda: self._zoom(0.1))
        QShortcut(QKeySequence.ZoomOut, self, activated=lambda: self._zoom(-0.1))
        QShortcut(QKeySequence("Ctrl+0"), self, activated=lambda: self._set_scale(1.0))

        self._refresh()
        self._show_transcript()  # разговор продолжается с того места, где закончился
        self.input.setFocus()

    # ------------------------------------------------------------ сборка окна --

    def _build_sidebar(self) -> QWidget:
        """Паспорт агента и его настройки: сущность видна как объект с состоянием."""
        panel = QFrame()
        panel.setObjectName("sidebar")
        self.sidebar = panel
        panel.setFixedWidth(round(300 * self.scale))
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)

        # Панель прокручивается: контролов много, и на большом масштабе они
        # перестают помещаться по высоте — без прокрутки Qt сжимал бы карточки.
        scroll = QScrollArea()
        scroll.setObjectName("sidebarScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setStyleSheet(f"background: {PANEL};")
        inner = QWidget()
        inner.setObjectName("sidebarInner")
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        lay = QVBoxLayout(inner)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(13)

        card = QFrame()
        card.setObjectName("card")
        card_lay = QHBoxLayout(card)
        card_lay.setContentsMargins(14, 14, 14, 14)
        card_lay.setSpacing(12)
        avatar = QLabel("🤖")
        self.avatar = avatar
        avatar.setObjectName("avatar")
        avatar.setFixedSize(round(44 * self.scale), round(44 * self.scale))
        avatar.setAlignment(Qt.AlignCenter)
        card_lay.addWidget(avatar, 0, Qt.AlignTop)
        who = QVBoxLayout()
        who.setSpacing(2)
        self.name_label = QLabel()
        self.name_label.setObjectName("agentName")
        self.role_label = QLabel()
        self.role_label.setObjectName("agentRole")
        self.role_label.setWordWrap(True)
        self.id_label = QLabel()
        self.id_label.setObjectName("agentId")
        who.addWidget(self.name_label)
        who.addWidget(self.role_label)
        who.addWidget(self.id_label)
        card_lay.addLayout(who, 1)

        # Паспорт можно поправить, не заводя нового агента: память при нём останется.
        edit = QPushButton("изменить")
        edit.setObjectName("link")
        edit.setCursor(Qt.PointingHandCursor)
        edit.setToolTip("Имя, подпись и инструкция агента")
        edit.clicked.connect(self._edit_profile)
        card_lay.addWidget(edit, 0, Qt.AlignTop)
        lay.addWidget(card)

        # Профиль пользователя — рядом с паспортом агента, потому что это его пара:
        # там «кто отвечает», здесь «кому и как». Профиль подключается к каждому
        # запросу, поэтому переключатель на виду, а не спрятан в отдельном окне.
        lay.addWidget(_section("ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ"))
        self.persona_box = QComboBox()
        self.persona_box.setToolTip(
            "Кто вы для агента и в какой форме просите отвечать: стиль, формат,\n"
            "ограничения. Профиль уходит в каждый запрос отдельным блоком.\n"
            "«Без профиля» — агент отвечает как умеет: удобно сравнить."
        )
        self.persona_box.currentIndexChanged.connect(self._on_persona)
        lay.addWidget(self.persona_box)
        self.persona_name = QLabel()
        self.persona_name.setObjectName("personaName")
        self.persona_name.setWordWrap(True)
        lay.addWidget(self.persona_name)
        self.persona_note = _wrapped("", "note")
        lay.addWidget(self.persona_note)
        persona_row = QHBoxLayout()
        persona_row.setSpacing(8)
        self.persona_edit_btn = _ghost("Изменить")
        self.persona_edit_btn.setToolTip("Стиль, формат, длина и ограничения — правятся на месте")
        self.persona_edit_btn.clicked.connect(self._edit_persona)
        new_persona = _ghost("+ профиль")
        new_persona.setToolTip("Завести второй профиль и сравнить ответы для разных людей")
        new_persona.clicked.connect(self._create_persona)
        persona_row.addWidget(self.persona_edit_btn, 1)
        persona_row.addWidget(new_persona, 1)
        lay.addLayout(persona_row)

        lay.addWidget(_section("МОДЕЛЬ"))
        self.model_box = QComboBox()
        for m in config.MODELS:
            self.model_box.addItem(m["label"], m["code"])
        self.model_box.currentIndexChanged.connect(self._on_model)
        lay.addWidget(self.model_box)

        lay.addWidget(_section("ТЕМПЕРАТУРА"))
        temp_row = QHBoxLayout()
        temp_row.setSpacing(10)
        self.temp = QSlider(Qt.Horizontal)
        self.temp.setRange(0, 195)  # шкала в сотых: диапазон Qwen — [0, 2)
        self.temp.valueChanged.connect(lambda v: self.temp_value.setText(f"{v / 100:.2f}"))
        self.temp.sliderReleased.connect(lambda: self._apply(temperature=self.temp.value() / 100))
        self.temp_value = QLabel()
        self.temp_value.setObjectName("agentId")
        self.temp_value.setFixedWidth(32)
        temp_row.addWidget(self.temp, 1)
        temp_row.addWidget(self.temp_value)
        lay.addLayout(temp_row)

        # Это и есть то самое N из подсказок стратегий. В поле пары, потому что пара —
        # неделимая единица памяти (вопрос без ответа модели бесполезен), а в подписи
        # сразу и то же число в сообщениях: считать в уме незачем.
        self.mem_caption = _section("ГЛУБИНА ПАМЯТИ, ПАР")
        lay.addWidget(self.mem_caption)
        self.mem_box = QSpinBox()
        self.mem_box.setRange(0, MEMORY_TURNS_MAX)
        self.mem_box.setToolTip(
            "Сколько последних пар «вопрос-ответ» уходит в модель дословно.\n"
            "Пара — это два сообщения, поэтому 10 пар = 20 сообщений истории."
        )
        self.mem_box.editingFinished.connect(lambda: self._apply(memory_turns=self.mem_box.value()))
        lay.addWidget(self.mem_box)

        # Стратегия контекста: что агент делает с тем, что выпало из окна памяти.
        # Три положения одного переключателя, подсказка под ним — про выбранную. Под
        # русской подписью — английское название из задания: по нему стратегию узнают
        # в чужих текстах. Второй строкой, а не через точку в той же: на масштабе 1.4
        # «Факты · Sticky Facts / Key-Value Memory» в ширину панели не влезает.
        # Модель памяти: три слоя, и в панели они показаны картой, а не набором
        # тумблеров. Управляется слой там, где он и настраивается (глубина памяти,
        # стратегия, свой тумблер) — дублировать эти элементы здесь незачем, зато
        # видно главное: что лежит в каждом слое и во что он обходится в запросе.
        lay.addWidget(_section("СЛОИ ПАМЯТИ"))
        self.layer_rows: dict[str, tuple[QLabel, QLabel]] = {}
        for item in config.LAYERS:
            head = QLabel()
            head.setObjectName("layerHead")
            head.setWordWrap(True)
            state = _wrapped("", "note")
            state.setToolTip(f"{item['en']} · {item['what']}\nхранение: {item['store']}\n"
                             f"включается: {item['control']}\n\n{item['hint']}")
            head.setToolTip(state.toolTip())
            lay.addWidget(head)
            lay.addWidget(state)
            self.layer_rows[item["code"]] = (head, state)

        # Единственный слой со своим выключателем: краткосрочная настраивается глубиной
        # памяти выше, долговременная — стратегией «Факты» ниже.
        self.working_box = QCheckBox("Рабочая память: вести задачу")
        working_hint = (
            "Агент ведёт карточку текущей задачи: цель, шаги, находки, созданные файлы,\n"
            "открытые вопросы. Она уходит в запрос, пока задача открыта, и пропадает\n"
            "из него, когда задача закрыта, — но остаётся в архиве."
        )
        self.working_box.setToolTip(working_hint)
        self.working_box.clicked.connect(lambda on: self._apply(working=on))
        lay.addWidget(self.working_box)

        lay.addWidget(_section("СТРАТЕГИЯ КОНТЕКСТА"))
        self.strategy_group = QButtonGroup(self)
        self.strategy_buttons: dict[str, QRadioButton] = {}
        for item in config.STRATEGIES:
            pair = QVBoxLayout()
            pair.setSpacing(1)
            button = QRadioButton(item["label"])
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _=False, code=item["code"]: self._apply(strategy=code))
            self.strategy_group.addButton(button)
            self.strategy_buttons[item["code"]] = button
            pair.addWidget(button)
            english = QLabel(item["en"])
            english.setObjectName("strategyEn")
            english.setToolTip(_strategy_hint(item["code"], config.AGENT_MEMORY_TURNS))
            pair.addWidget(english)
            lay.addLayout(pair)
        self.strategy_hint = QLabel()
        self.strategy_hint.setObjectName("note")
        self.strategy_hint.setWordWrap(True)
        lay.addWidget(self.strategy_hint)

        # Сжатие истории — не стратегия, а ОПЦИЯ: суммаризация включается поверх любой
        # из трёх стратегий, поэтому у неё свой раздел и свой тумблер, а не четвёртое
        # положение переключателя. Период суммаризации, как и всё остальное, проверяет
        # агент — поле пускает любое число, а отказ объясняет он.
        lay.addWidget(_section("СЖАТИЕ ИСТОРИИ"))
        self.summarize_box = QCheckBox("Суммаризировать старую историю")
        summarize_hint = (
            "То, что выпало из окна памяти, копится и каждые N сообщений сворачивается\n"
            "отдельным вызовом модели в суммаризацию — она уходит в запрос вместо самих\n"
            "сообщений. Работает вместе с любой стратегией."
        )
        self.summarize_box.setToolTip(summarize_hint)
        self.summarize_box.clicked.connect(lambda on: self._apply(summarize=on))
        lay.addWidget(self.summarize_box)
        self.summarize_note = _wrapped("", "note")
        self.summarize_note.setToolTip(summarize_hint)
        lay.addWidget(self.summarize_note)
        # В поле только число, единица — в подписи: как у «потолка шагов». Подпись
        # короткая: с «период суммаризации, сообщ.» строка вместе с полем не помещалась
        # в ширину панели и обрезалась на масштабе пользователя, а раздел и без того
        # называется «СЖАТИЕ ИСТОРИИ».
        every_row = QHBoxLayout()
        every_row.setSpacing(8)
        self.every_label = QLabel("период, сообщ.")
        self.every_label.setObjectName("agentRole")
        hint = (
            "Через сколько сообщений за окном памяти обновлять суммаризацию:\n"
            "накопилось столько — агент сворачивает их отдельным вызовом.\n"
            "Границы проверяет агент и объясняет отказ сам."
        )
        self.every_label.setToolTip(hint)
        self.every_box = QSpinBox()
        self.every_box.setRange(0, 999)
        self.every_box.setFixedWidth(72)
        self.every_box.setToolTip(hint)
        self.every_box.editingFinished.connect(lambda: self._apply(summary_every=self.every_box.value()))
        every_row.addWidget(self.every_label, 1)
        every_row.addWidget(self.every_box)
        lay.addLayout(every_row)

        # Лимит ответа — настоящий предел генерации у модели. Поставьте маленький
        # и увидите, что бывает при нехватке токенов: ответ обрывается на полуслове.
        # Поле НАРОЧНО пускает больше потолка модели: границу проверяет агент, и
        # пусть он сам объяснит отказ — интерфейсу дублировать его правила незачем.
        self.answer_caption = _section("ЛИМИТ ОТВЕТА, ТОКЕНОВ")
        lay.addWidget(self.answer_caption)
        self.answer_box = QSpinBox()
        self.answer_box.setRange(0, 999_999)
        self.answer_box.setSingleStep(64)
        # Подписи «без ограничения» вместо нуля здесь нарочно нет: со спецтекстом Qt
        # запрещает набрать само минимальное значение, и ноль стало бы невозможно
        # ввести с клавиатуры. Что значит ноль — сказано строкой ниже.
        answer_hint = (
            "Сколько токенов модель может сгенерировать в ответ.\n"
            "0 — не ограничивать. Маленькое значение обрывает ответ на полуслове,\n"
            "значение выше потолка модели агент отклонит и скажет, почему."
        )
        self.answer_box.setToolTip(answer_hint)
        self.answer_box.editingFinished.connect(lambda: self._apply(max_tokens=self.answer_box.value()))
        lay.addWidget(self.answer_box)
        self.answer_note = _wrapped("", "note")
        self.answer_note.setToolTip(answer_hint)
        lay.addWidget(self.answer_note)

        lay.addWidget(_section("КАК АГЕНТ РАБОТАЕТ"))
        self.tools_box = QCheckBox("Инструменты")
        self.tools_box.clicked.connect(lambda on: self._apply(tools_enabled=on))
        lay.addWidget(self.tools_box)
        self.plan_box = QCheckBox("Планировать действия")
        self.plan_box.clicked.connect(lambda on: self._apply(planning=on))
        lay.addWidget(self.plan_box)

        steps_row = QHBoxLayout()
        steps_row.setSpacing(8)
        steps_label = QLabel("потолок шагов")
        steps_label.setObjectName("agentRole")
        self.steps_box = QSpinBox()
        self.steps_box.setRange(1, 12)
        self.steps_box.setFixedWidth(64)
        self.steps_box.editingFinished.connect(lambda: self._apply(max_steps=self.steps_box.value()))
        steps_row.addWidget(steps_label, 1)
        steps_row.addWidget(self.steps_box)
        lay.addLayout(steps_row)

        tools_btn = _ghost("Чем агент умеет действовать")
        tools_btn.clicked.connect(lambda: ToolsDialog(self.agent, self).exec())
        lay.addWidget(tools_btn)

        # Файлы агент кладёт в песочницу, и путь к ней был виден только мелкой строкой
        # внутри окна инструментов — из-за этого «а где посмотреть, что он записал?»
        # оказывался неочевидным вопросом. Теперь папка открывается одним нажатием.
        self.workspace_btn = _ghost("Открыть рабочую папку")
        self.workspace_btn.clicked.connect(self._open_workspace)
        lay.addWidget(self.workspace_btn)

        lay.addStretch(1)

        # Контекст показываем ДО отправки: сколько токенов уйдёт следующим запросом
        # и из чего они складываются. Обновляется после каждой правки настроек.
        lay.addWidget(_section("КОНТЕКСТ СЛЕДУЮЩЕГО ЗАПРОСА"))
        self.context_bar = ContextBar()
        lay.addWidget(self.context_bar)

        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.turns_stat, turns_card = _stat("обращений")
        self.mem_stat, mem_card = _stat("в памяти")
        self.hist_stat, hist_card = _stat("в истории")
        stats.addWidget(turns_card)
        stats.addWidget(mem_card)
        stats.addWidget(hist_card)
        lay.addLayout(stats)

        tokens_btn = _ghost("Токены и стоимость")
        tokens_btn.setToolTip("Как растут токены и цена по мере диалога")
        tokens_btn.clicked.connect(lambda: TokensDialog(self.agent, self, self.agents).exec())
        lay.addWidget(tokens_btn)

        memory_btn = _ghost("Память и история")
        memory_btn.clicked.connect(lambda: MemoryDialog(self.agent, self).exec())
        lay.addWidget(memory_btn)

        reset_btn = _ghost("Забыть разговор")
        reset_btn.setToolTip("Очистит и память агента, и его историю на диске")
        reset_btn.clicked.connect(self._reset)
        lay.addWidget(reset_btn)

        self.storage_note = QLabel()
        self.storage_note.setObjectName("note")
        self.storage_note.setWordWrap(True)
        lay.addWidget(self.storage_note)
        return panel

    def _build_main(self) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Полоса агентов: переключение вынесено наверх, чтобы длинный список
        # уходил вбок и не выдавливал настройки в боковой панели.
        self.agent_bar = QWidget()
        self.agent_bar.setObjectName("agentBarRow")
        self.agent_bar.setFixedHeight(round(52 * self.scale))
        bar_row = QHBoxLayout(self.agent_bar)
        bar_row.setContentsMargins(0, 0, 16, 0)
        bar_row.setSpacing(10)

        tabs = QScrollArea()
        tabs.setObjectName("agentBar")
        tabs.setWidgetResizable(True)
        tabs.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        bar_inner = QWidget()
        bar_inner.setObjectName("agentBarInner")
        self.agent_list = QHBoxLayout(bar_inner)
        self.agent_list.setContentsMargins(16, 8, 8, 8)
        self.agent_list.setSpacing(8)
        tabs.setWidget(bar_inner)
        bar_row.addWidget(tabs, 1)

        # Кнопка вне прокрутки: сколько бы ни было агентов, она всегда на виду.
        new_agent = QPushButton("+ Новый агент")
        new_agent.setObjectName("newAgent")
        new_agent.setCursor(Qt.PointingHandCursor)
        new_agent.clicked.connect(self._create_agent)
        bar_row.addWidget(new_agent)

        lay.addWidget(self.agent_bar)

        # Полоса веток: показывается только в стратегии «ветки диалога». Слева
        # ветки активного агента (основная всегда первая), справа — действия.
        self.branch_bar = QWidget()
        self.branch_bar.setObjectName("branchBarRow")
        self.branch_bar.setFixedHeight(round(46 * self.scale))
        branch_row = QHBoxLayout(self.branch_bar)
        branch_row.setContentsMargins(16, 0, 16, 0)
        branch_row.setSpacing(10)
        caption = QLabel("ВЕТКИ")
        caption.setObjectName("branchCaption")
        branch_row.addWidget(caption)
        branch_tabs = QScrollArea()
        branch_tabs.setObjectName("branchBar")
        branch_tabs.setWidgetResizable(True)
        branch_tabs.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        branch_inner = QWidget()
        branch_inner.setObjectName("branchBarInner")
        self.branch_list = QHBoxLayout(branch_inner)
        self.branch_list.setContentsMargins(0, 6, 8, 6)
        self.branch_list.setSpacing(8)
        branch_tabs.setWidget(branch_inner)
        branch_row.addWidget(branch_tabs, 1)
        # Отдельной кнопки «поставить точку» нет: точка ветвления — не самостоятельное
        # действие, а место развилки, и агент ставит её сам в момент создания ветки.
        # В ленте она при этом видна меткой ⚑, а в списке «Новой ветки» — строкой, от
        # которой можно отвести ещё одну ветку.
        fork_btn = QPushButton("⑂ Новая ветка")
        fork_btn.setObjectName("branchAction")
        fork_btn.setCursor(Qt.PointingHandCursor)
        fork_btn.setToolTip("Отвести отдельную ветку: от этого места или от прежней точки ветвления")
        fork_btn.clicked.connect(self._fork)
        branch_row.addWidget(fork_btn)
        self.branch_bar.hide()
        lay.addWidget(self.branch_bar)

        # Полоса этапов задачи — конечный автомат рабочей памяти на виду. Видна,
        # только пока задача открыта: нет задачи — нет и автомата.
        self.state_bar = QWidget()
        self.state_bar.setObjectName("stateBarRow")
        self.state_bar.setFixedHeight(round(78 * self.scale))
        # Полоса в два ряда: сверху сам автомат (этапы и действия человека), снизу
        # состояние задачи словами. В один ряд это не ставится: четыре чипа и две
        # кнопки на масштабе 1.4 съедают всю ширину, и подписям остаются крохи.
        state_box = QVBoxLayout(self.state_bar)
        state_box.setContentsMargins(16, 4, 16, 4)
        state_box.setSpacing(2)
        state_row = QHBoxLayout()
        state_row.setContentsMargins(0, 0, 0, 0)
        state_row.setSpacing(10)
        state_box.addLayout(state_row)
        state_caption = QLabel("ЭТАП")
        state_caption.setObjectName("stateCaption")
        state_row.addWidget(state_caption)
        self.state_list = QHBoxLayout()
        self.state_list.setContentsMargins(0, 0, 0, 0)
        self.state_list.setSpacing(6)
        state_row.addLayout(self.state_list)
        state_row.addStretch(1)
        # Состояние задачи — три величины, и каждая подписана: этап чипами сверху,
        # шаг и ожидаемое действие — двумя подписанными частями снизу. В одну строку
        # без подписей они не ставятся: «шаг 2 из 4 · сверстать · агент» читалось бы
        # как набор слов.
        state_lines = QHBoxLayout()
        state_lines.setContentsMargins(0, 0, 0, 0)
        state_lines.setSpacing(14)
        state_box.addLayout(state_lines)
        # Обе подписи сами вписываются в свою долю ширины: полоса от них не растёт.
        self.state_progress = ElidedLabel("stateProgress")
        state_lines.addWidget(self.state_progress, 1)
        self.state_expect = ElidedLabel("stateExpect")
        state_lines.addWidget(self.state_expect, 1)
        self.pause_btn = QPushButton("Пауза")
        self.pause_btn.setObjectName("branchAction")
        self.pause_btn.setCursor(Qt.PointingHandCursor)
        self.pause_btn.clicked.connect(self._toggle_pause)
        state_row.addWidget(self.pause_btn)
        self.close_task_btn = QPushButton("Завершить задачу")
        self.close_task_btn.setObjectName("branchAction")
        self.close_task_btn.setCursor(Qt.PointingHandCursor)
        self.close_task_btn.setToolTip(
            "Перевести задачу на этап «Готово». Правила те же, что у модели:\n"
            "с этапа планирования агент откажет и скажет, куда перейти можно.\n"
            "Итог завершённой задачи останется в долговременной памяти."
        )
        self.close_task_btn.clicked.connect(self._close_task)
        state_row.addWidget(self.close_task_btn)
        self.state_bar.hide()
        lay.addWidget(self.state_bar)

        header = QFrame()
        header.setObjectName("header")
        head_lay = QHBoxLayout(header)
        head_lay.setContentsMargins(24, 14, 20, 14)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = QLabel()
        self.title.setObjectName("title")
        titles.addWidget(self.title)
        # Строка про историю: видно, что разговор не начинается заново каждый запуск.
        self.subtitle = QLabel()
        self.subtitle.setObjectName("subtitle")
        titles.addWidget(self.subtitle)
        head_lay.addLayout(titles, 1)
        # Рабочая память видна прямо в шапке: над какой задачей агент сейчас
        # работает и на каком шаге. Сам автомат — полосой выше.
        self.task_line = QLabel()
        self.task_line.setObjectName("taskLine")
        self.task_line.setWordWrap(True)
        self.task_line.hide()
        head_lay.addWidget(self.task_line, 0)
        head_lay.addSpacing(10)
        self.dot = QLabel()
        self.dot.setFixedSize(9, 9)
        self.status = QLabel()
        self.status.setObjectName("status")
        head_lay.addWidget(self.dot)
        head_lay.addSpacing(7)
        head_lay.addWidget(self.status)
        lay.addWidget(header)

        self.chat = ChatView()
        lay.addWidget(self.chat, 1)

        self.examples = QScrollArea()
        self.examples.setWidgetResizable(True)
        self.examples.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.examples.setFixedHeight(round(52 * self.scale))
        strip = QWidget()
        strip_lay = QHBoxLayout(strip)
        strip_lay.setContentsMargins(24, 8, 20, 8)
        strip_lay.setSpacing(8)
        for example in config.EXAMPLES:
            chip = QPushButton(example["title"])
            chip.setObjectName("example")
            chip.setCursor(Qt.PointingHandCursor)
            chip.setToolTip(
                example["prompt"] + "\n\n"
                + (f"инструменты: {', '.join(example['tools'])}" if example["tools"]
                   else "без инструментов: ответ из памяти агента")
            )
            chip.clicked.connect(lambda _, text=example["prompt"]: self._use_example(text))
            strip_lay.addWidget(chip)
        strip_lay.addStretch(1)
        self.examples.setWidget(strip)
        lay.addWidget(self.examples)

        composer = QFrame()
        composer.setObjectName("composer")
        comp_lay = QHBoxLayout(composer)
        comp_lay.setContentsMargins(24, 16, 20, 16)
        comp_lay.setSpacing(12)
        self.input = Composer()
        self.input.setObjectName("input")
        self.input.setPlaceholderText("Сообщение агенту…")
        self.input.setFixedHeight(56)
        self.input.submitted.connect(lambda: self._send())
        # «Сравнить» — тот же вопрос, но с двумя ответами: настоящий (суммаризация + окно)
        # и теневой, с полной историей как есть. Рядом видно и качество, и цену.
        self.compare_btn = _ghost("Сравнить")
        self.compare_btn.setFixedHeight(56)
        self.compare_btn.setToolTip(
            "Ответить как обычно и рядом показать теневой ответ на тот же вопрос\n"
            "с полной историей вместо суммаризации: два ответа и два счёта в токенах.\n"
            "В память идёт только настоящий ответ."
        )
        self.compare_btn.clicked.connect(lambda: self._send(compare=True))
        # Вторая ось сравнения — профиль: тот же вопрос и тот же контекст, но
        # требования к ответу другие. Второй профиль выбирается меню на кнопке, а
        # не ещё одним списком в панели: список профилей там уже есть.
        self.persona_btn = _ghost("⇄ профили")
        self.persona_btn.setFixedHeight(56)
        self.persona_btn.setToolTip(
            "Ответить на тот же вопрос дважды: текущим профилем и выбранным.\n"
            "Контекст и память одинаковые, разница только в профиле — видно, что\n"
            "персонализация меняет сам ответ. Стоит один дополнительный вызов модели."
        )
        self.persona_btn.clicked.connect(self._compare_personas)
        self.send_btn = QPushButton("Отправить")
        self.send_btn.setObjectName("send")
        self.send_btn.setFixedHeight(56)
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.clicked.connect(lambda: self._send())
        comp_lay.addWidget(self.input, 1)
        comp_lay.addWidget(self.persona_btn)
        comp_lay.addWidget(self.compare_btn)
        comp_lay.addWidget(self.send_btn)
        lay.addWidget(composer)

        self._set_status(OK, "готов")
        return wrap

    # ----------------------------------------------------------------- вывод --

    def _set_status(self, color: str, text: str) -> None:
        self._status_color = color
        radius = max(2, round(4 * self.scale))
        self.dot.setStyleSheet(f"background: {color}; border-radius: {radius}px;")
        self.status.setText(text)

    def _refresh(self) -> None:
        """Перечитать паспорт активного агента и обновить панель и список агентов."""
        self._render_agents()
        p = self.agent.passport()
        self.name_label.setText(p["name"])
        self.role_label.setText(p["role"])
        self.id_label.setText(f"id {p['id']}")
        self.title.setText(f"Диалог с агентом «{p['name']}»")
        self.turns_stat.setText(str(p["turns"]))
        self.mem_stat.setText(str(p["memory_messages"]))
        self.hist_stat.setText(str(p["history_messages"]))

        history_file = p["history_file"]
        branch = (
            f" · ветка «{p['branch_name']}» от точки «{p['branch_origin']}»"
            if p["branch"] != MAIN_BRANCH else ""
        )
        self.subtitle.setText(
            (f"история: {p['history_messages']} сообщ. на диске · последний разговор {_when(p['last_seen_at'])}"
             if p["history_messages"] else "история пуста — разговор начинается")
            + f" · стратегия: {p['strategy_label'].lower()}"
            + (" · сжатие: суммаризация" if p["summarize"] else " · без сжатия")
            + branch
        )
        # Задача и её автомат — только пока она открыта: в этом и смысл рабочего слоя.
        task = p["task"]
        active_task = bool(p["task_active"])
        self.task_line.setVisible(active_task)
        self.state_bar.setVisible(active_task)
        if active_task:
            self.task_line.setText(
                ("⏸ " if task["paused"] else "⌛ ")
                + f"{task['title'] or task['goal']} · {task['state_label'].lower()} · "
                + f"шаг {task['step']} из {task['total']}"
                + (" · на паузе" if task["paused"] else "")
            )
            self.task_line.setToolTip(
                (f"Задача отложена на этапе «{task['state_label']}».\n"
                 f"Ждём: {task['expect_line']}\n\n" if task["paused"] else
                 f"Ждём: {task['expect_line']}\n\n") + task["text"]
            )
            self._render_states(task)
        # Полоса веток нужна только в стратегии веток; в остальных ветка видна подписью.
        self.branch_bar.setVisible(p["strategy"] == "branches")
        if p["strategy"] == "branches":
            self._render_branches()
        self.storage_note.setText(
            "Память живёт внутри агента, а не в окне, и переживает перезапуск: "
            f"переписка и настройки пишутся в {Path(history_file).name if history_file else 'память процесса'}."
        )
        self.storage_note.setToolTip(history_file or "")

        # Вес контекста считает агент — окно только рисует полосу.
        self.context_bar.show_state(self.agent.tokens_state())

        self._loading = True
        # Профиль пользователя: список общий для всех агентов, подключён — у каждого
        # свой. Пункт «без профиля» стоит последним: это не профиль, а его отсутствие.
        self.persona_box.clear()
        for item in self.agent.personas():
            self.persona_box.addItem(item["name"], item["id"])
            self.persona_box.setItemData(self.persona_box.count() - 1, item["text"], Qt.ToolTipRole)
        self.persona_box.addItem("— без профиля —", "")
        self.persona_box.setCurrentIndex(max(0, self.persona_box.findData(p["persona_id"])))
        who = p["persona"]
        self.persona_edit_btn.setEnabled(bool(who))
        if who:
            self.persona_name.setText(f'<span style="color: {PERSONA}">■</span> {who["name"]}')
            # По пункту на строку: в ширину панели состав одной строкой не влезает,
            # и перенос рвёт пары «поле: значение» пополам — «длина:» остаётся на
            # одной строке, «коротко» уезжает на следующую. Цена — отдельной строкой:
            # это не часть состава.
            self.persona_note.setText(
                "\n".join(who["parts"])
                + f"\nвес в запросе: {_num(p['persona_tokens'])} т., в каждом"
            )
            self.persona_note.setToolTip(who["text"])
            self.persona_name.setToolTip(who["text"])
        else:
            self.persona_name.setText("профиль не подключён")
            self.persona_note.setText(
                "Форму ответа агент выбирает сам. Подключите профиль — и стиль, формат, "
                "длина и ограничения станут вашими."
            )
            self.persona_note.setToolTip("")
            self.persona_name.setToolTip("")
        self.model_box.setCurrentIndex(max(0, self.model_box.findData(p["model"])))
        self.temp.setValue(round(p["temperature"] * 100))
        self.temp_value.setText(f"{p['temperature']:.2f}")
        self.mem_box.setValue(p["memory_turns"])
        self.mem_caption.setText(f"ГЛУБИНА ПАМЯТИ, ПАР · {p['memory_turns'] * 2} СООБЩ.")
        button = self.strategy_buttons.get(p["strategy"])
        if button is not None:
            button.setChecked(True)
        # В подсказках стратегий стоит настоящая глубина памяти, а не абстрактное N:
        # иначе неясно, где эти пары задаются. То же и в подсказках самих кнопок.
        for code, radio in self.strategy_buttons.items():
            radio.setToolTip(_strategy_hint(code, p["memory_turns"]))
        long = p["long"]
        extra = ""
        if p["strategy"] == "facts":
            extra = (f" Сейчас записей: {long['count']} (версия №{long['version']})."
                     if long["count"] else " Записей пока нет — появятся после первого ответа.")
        elif p["strategy"] == "branches":
            extra = f" Активна ветка «{p['branch_name']}»."
        self.strategy_hint.setText(_strategy_hint(p["strategy"], p["memory_turns"]) + extra)

        # Карта слоёв: у каждого своё состояние и свой вес в следующем запросе.
        self.working_box.setChecked(p["working"])
        for layer in p["layers"]:
            head, state = self.layer_rows[layer["code"]]
            color = LAYER_COLORS.get(layer["code"], ACCENT) if layer["active"] else MUTED
            # Вес слоя — во второй строке, а не рядом с названием: вместе они не
            # помещались в ширину панели и переносились на две строки.
            head.setText(f'<span style="color: {color}">■</span> {layer["label"].upper()}')
            state.setText(
                layer["state"]
                + (f" · {_num(layer['tokens'])} т. в запросе" if layer["tokens"] else "")
                + (f" · {layer['control']}" if not layer["active"] else "")
            )
        # Период суммаризации имеет смысл только при включённом сжатии — иначе поле спит.
        self.summarize_box.setChecked(p["summarize"])
        self.every_box.setEnabled(p["summarize"])
        self.every_label.setEnabled(p["summarize"])
        self.every_box.setValue(p["summary_every"])
        summary = p["summary"]
        self.summarize_note.setText(
            ("Выпавшее из окна памяти не теряется: оно уходит в модель короткой суммаризацией "
             "вместо самих сообщений."
             + (f" Сейчас это суммаризация №{summary['version']} вместо {summary['messages']} сообщ."
                if summary["version"] else " Суммаризация появится, когда за окном накопится столько "
                                           "сообщений, сколько стоит в поле ниже.")
             if p["summarize"] else
             "Сжатие выключено: то, что выпало из окна памяти, в модель уже не возвращается."
             + (f" Суммаризация №{summary['version']} осталась в базе и вернётся, если включить."
                if summary["version"] else ""))
            + " Это опция, а не стратегия, — работает с любой из трёх."
        )
        self.tools_box.setChecked(p["tools_enabled"])
        self.plan_box.setChecked(p["planning"])
        self.steps_box.setValue(p["max_steps"])
        # Потолок генерации у каждой модели свой: показываем его в подписи, но ввод
        # не ограничиваем — за границу отвечает агент (и объясняет отказ словами).
        self.answer_caption.setText(
            f"ЛИМИТ ОТВЕТА · ПОТОЛОК {_num(config.model_max_output(p['model']))}"
        )
        self.answer_box.setValue(p["max_tokens"] or 0)
        self.answer_note.setText(
            "0 — без ограничения: сколько модель сочтёт нужным, столько и напишет."
            if not p["max_tokens"] else
            f"Ответ оборвётся на {_num(p['max_tokens'])} токенах. Поставьте 0, чтобы снять ограничение."
        )
        self._loading = False

    def _show_meta(self, reply: AgentReply) -> None:
        """Строки под ответом: метрики, счёт в токенах и ссылка на сырой обмен."""
        u = reply.usage or {}
        cost = f" · ${reply.cost_usd:.6f} (free-квота)" if reply.cost_usd is not None else ""
        row = QWidget()
        outer = QVBoxLayout(row)
        outer.setContentsMargins(6, 0, 0, 4)
        outer.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)
        name = config.model_label(reply.model).split(" · ")[0]
        meta = _wrapped(
            f"{name} · {reply.elapsed_s} c · итого за обращение "
            f"{_num(u.get('prompt_tokens', 0))}→{_num(u.get('completion_tokens', 0))} токенов{cost} · "
            f"обращение #{reply.turn} · "
            f"{f'{len(reply.steps)} шаг(ов) инструментами' if reply.steps else 'без инструментов'} · "
            f"{reply.llm_calls} вызов(ов) модели",
            "meta",
        )
        link = QPushButton("сырой обмен →")
        link.setObjectName("link")
        link.setCursor(Qt.PointingHandCursor)
        link.clicked.connect(lambda: RawDialog(reply, self).exec())
        top.addWidget(meta, 1)      # метрики занимают всю строку,
        top.addWidget(link, 0)      # ссылка прижата к правому краю
        outer.addLayout(top)

        # Вторая строка — счёт за обращение: из чего сложился запрос, сколько занял
        # ответ и насколько собственная оценка агента разошлась с фактом.
        t = reply.tokens
        if t:
            b = t.breakdown
            error = f"{t.error_pct:+.1f}%" if t.error_pct is not None else "—"
            summary = (f" + профиль {_num(b.persona)}" if b.persona else "") + \
                      (f" + суммаризация {_num(b.summary)}" if b.summary else "")
            layers = (f" + долговременная {_num(b.long)}" if b.long else "") + \
                     (f" + задача {_num(b.task)}" if b.task else "")
            outer.addWidget(_wrapped(
                f"запрос {_num(t.prompt_tokens)} т. = инструкция {_num(b.system)}{summary}{layers} + память "
                f"{_num(b.memory)} + вопрос {_num(b.question)} + схемы {_num(b.tools)} · "
                f"ответ {_num(t.completion_tokens)} т. · оценка до отправки {_num(t.estimated)} "
                f"({error}) · контекст занят на {t.fill * 100:.2f}% от {_num(t.limit)}",
                "meta",
            ))

            # Третья строка — цена стратегии: что осталось за окном, чем это заменено и
            # сколько весил бы тот же запрос с полной историей. Это «до/после» на каждом
            # ответе. Четвёртая, отдельная, — про сжатие: оно опция, а не стратегия, и
            # включается поверх любой из них, поэтому и в строку с ней не мешается.
            for line, color in (
                (_strategy_line(t, self.agent.passport()["branch_name"]),
                 STRATEGY_COLORS.get(t.strategy, MUTED)),
                (_compression_line(t), SUMMARY),
                (_layers_line(t), TASK),
                # Пятая строка — персонализация: какой профиль ушёл в запрос,
                # во что обошёлся и сошёлся ли с ним ответ.
                (_persona_line(t, reply.persona), PERSONA),
            ):
                if not line:
                    continue
                label = _wrapped(line, "meta")
                label.setStyleSheet(f"color: {color}; font-size: 11px; background: transparent;")
                outer.addWidget(label)

            # Нарушённый инвариант — не предупреждение «на всякий случай», а факт:
            # правило записано в памяти, ушло в запрос и всё равно нарушено. Молчать
            # об этом нельзя, иначе инвариант так и останется просьбой.
            if reply.violations:
                broken = _wrapped("⛔ нарушены инварианты — " + " · ".join(reply.violations)
                                  + ". Ответ оставлен как есть: решать вам.", "meta")
                broken.setStyleSheet(f"color: {ERR_TEXT}; font-size: 11px; background: transparent;")
                outer.addWidget(broken)

            # Расхождение с профилем — такой же факт, как нарушенный инвариант:
            # требование ушло в запрос и всё равно не выполнено. Ответ не
            # перегенерируется, но молчать об этом нельзя.
            if reply.persona and reply.persona.broken:
                missed = _wrapped(
                    "⛔ ответ разошёлся с профилем — "
                    + " · ".join(f"{check.label}: {check.detail}" for check in reply.persona.broken)
                    + ". Ответ оставлен как есть.", "meta",
                )
                missed.setStyleSheet(f"color: {ERR_TEXT}; font-size: 11px; background: transparent;")
                outer.addWidget(missed)

            trouble = []
            if t.trimmed_pairs:
                trouble.append(
                    f"контекст переполнен: из окна памяти выброшено {t.trimmed_pairs} пар(ы) — "
                    "начало разговора в модель уже не ушло (в истории оно осталось)"
                )
            if t.truncated:
                trouble.append(
                    f"ответ оборван по лимиту генерации ({_num(t.reserve)} токенов): "
                    "модели не хватило места договорить"
                )
            if trouble:
                warning = _wrapped("⚠ " + " · ".join(trouble), "meta")
                warning.setStyleSheet(f"color: {WARN}; font-size: 11px; background: transparent;")
                outer.addWidget(warning)

        self.chat.add_row(row)

    def _show_trace(self, reply: AgentReply) -> None:
        """План, шаги, сжатие истории и маршрутизация памяти — то, чего в чате не бывает."""
        personal = reply.persona and (reply.persona.changes or reply.persona.rejected)
        if (not reply.plan and not reply.steps and not reply.compression and not reply.memory
                and not personal):
            return
        card = QFrame()
        card.setObjectName("trace")
        card.setMinimumWidth(min(560, self.chat.bubble_max))
        card.setMaximumWidth(self.chat.bubble_max + 60)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)

        if reply.plan:
            lay.addWidget(_trace_label("ПЛАН АГЕНТА"))
            for number, item in enumerate(reply.plan, 1):
                line = QLabel(f"{number}. {item}")
                line.setObjectName("planItem")
                line.setWordWrap(True)
                lay.addWidget(line)

        if reply.steps:
            lay.addWidget(_trace_label("ВЫПОЛНЕННЫЕ ШАГИ"))
            for step in reply.steps:
                arguments = ", ".join(
                    f"{k}={json.dumps(v, ensure_ascii=False)[:60]}" for k, v in step.arguments.items()
                )
                head = QLabel(
                    f"#{step.number}  {step.tool}({arguments})  ·  {step.title}"
                    f"{'' if step.ok else '  ·  ошибка'}  ·  {step.elapsed_s} c"
                )
                head.setObjectName("stepHead" if step.ok else "stepHeadErr")
                head.setWordWrap(True)
                lay.addWidget(head)

                box = QFrame()
                box.setObjectName("stepResult")
                box_lay = QVBoxLayout(box)
                box_lay.setContentsMargins(10, 7, 10, 8)
                result = QLabel(_clip(step.result, 700))
                result.setWordWrap(True)
                result.setTextInteractionFlags(Qt.TextSelectableByMouse)
                box_lay.addWidget(result)
                lay.addWidget(box)

        # Сжатие случается после ответа: выпавшие из окна сообщения набрались на
        # суммаризацию, и агент ещё одним вызовом свернул их. Показываем саму суммаризацию —
        # именно она уйдёт в следующий запрос вместо тех сообщений.
        if reply.compression:
            c = reply.compression
            lay.addWidget(_trace_label("СЖАТИЕ ИСТОРИИ"))
            head = QLabel(
                f"после ответа: {c.messages} сообщ. ≈ {_num(c.before)} т. свёрнуты в суммаризацию "
                f"№{c.version} ≈ {_num(c.after)} т.  ·  вызов модели {c.elapsed_s} c, "
                f"{_num(c.call_tokens)} т."
            )
            head.setObjectName("summaryHead")
            head.setWordWrap(True)
            lay.addWidget(head)
            box = QFrame()
            box.setObjectName("stepResult")
            box_lay = QVBoxLayout(box)
            box_lay.setContentsMargins(10, 7, 10, 8)
            text = QLabel(_clip(c.text, 900))
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            box_lay.addWidget(text)
            lay.addWidget(box)

        # Маршрутизация — главный блок дня: что из этого обмена в какой слой положено
        # и по чьему решению. Показываем не содержимое слоёв, а именно движение: по нему
        # видно, что выбор «что куда» делается явно, а не «само как-нибудь запомнится».
        if reply.memory:
            m = reply.memory
            lay.addWidget(_trace_label("МАРШРУТИЗАЦИЯ ПАМЯТИ"))
            how = (f"вызов модели {m.elapsed_s} c, {_num(m.call_tokens)} т."
                   if m.called else "без вызова модели — по правилам агента")
            # Каждый переход приходит строкой «A → B», и склейка через « → » давала
            # «A → B → B → C»: конец одного и начало следующего — один и тот же этап.
            path = _states_path(m.moved)
            head = QLabel(
                f"{len(m.routes)} запис(и) по слоям  ·  долговременная {m.long_items} зап. "
                f"≈ {_num(m.long_tokens)} т.  ·  задача "
                f"{(m.task_action or 'без изменений') if m.task else 'не заведена'}"
                f"{f' ≈ {_num(m.task_tokens)} т.' if m.task_tokens else ''}"
                + (f"  ·  этапы: {path}" if path else "")
                + (f"  ·  {m.paused}" if m.paused else "")
                + f"  ·  по {m.messages} сообщ.  ·  {how}"
            )
            head.setObjectName("taskHead")
            head.setWordWrap(True)
            lay.addWidget(head)
            box = QFrame()
            box.setObjectName("stepResult")
            box_lay = QVBoxLayout(box)
            box_lay.setContentsMargins(10, 7, 10, 8)
            sign = {"add": "+", "change": "~", "remove": "−", "keep": "=", "reject": "✕"}
            lines = [f"{sign.get(r.action, '·')} [{config.layer_label(r.layer).lower()}] {r.what} "
                     f"({config.note_source_label(r.source)})" for r in m.routes]
            # Краткосрочный слой в трассе маршрутизатора не участвует — он наполняется
            # правилом всегда, и строка про него стоит первой, чтобы карта была полной.
            lines.insert(0, "+ [краткосрочная] вопрос и ответ — 2 сообщ. в окно (правило)")
            text = QLabel(_clip("\n".join(lines), 1200))
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            box_lay.addWidget(text)
            lay.addWidget(box)

            # Отказ в переходе — самое интересное, что может случиться с автоматом:
            # модель попросила этап, а код не дал. Показываем это отдельной строкой,
            # цветом ошибки, чтобы не потерялось среди обычных записей.
            if m.rejected:
                refused = QLabel("✕ " + m.rejected)
                refused.setObjectName("stepHeadErr")
                refused.setWordWrap(True)
                lay.addWidget(refused)

        # Персонализация: что изменилось в профиле после этого обмена и кто это
        # решил. Тем же блоком видно и отказы — значение не из реестра профиль не
        # принимает, как автомат не принимает запрещённый переход.
        if personal:
            p = reply.persona
            lay.addWidget(_trace_label("ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ"))
            head = QLabel(
                f"«{p.name}» · {len(p.changes)} правк(и) по разговору ≈ {_num(p.tokens)} т. в запросе"
            )
            head.setObjectName("personaHead")
            head.setWordWrap(True)
            lay.addWidget(head)
            box = QFrame()
            box.setObjectName("stepResult")
            box_lay = QVBoxLayout(box)
            box_lay.setContentsMargins(10, 7, 10, 8)
            sign = {"add": "+", "change": "~", "keep": "=", "reject": "✕"}
            lines = [f"{sign.get(change.action, '·')} {change.what} "
                     f"({config.persona_source_label(change.source)})" for change in p.changes]
            lines += [f"✕ {text}" for text in p.rejected]
            text = QLabel(_clip("\n".join(lines), 900))
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            box_lay.addWidget(text)
            lay.addWidget(box)

        self.chat.add(card, Qt.AlignLeft)

    def _show_shadow(self, reply: AgentReply) -> None:
        """Теневой ответ под настоящим: два ответа и два счёта рядом.

        Сравнений два — по контексту (полная история вместо стратегии) и по
        профилю (тот же контекст, другие требования к ответу), и показываются они
        по-разному: в первом случае интересна цена, во втором — сам ответ.
        """
        s, t = reply.shadow, reply.tokens
        if s.kind == "persona":
            self._show_persona_shadow(reply)
            return
        strategy = config.strategy_label(t.strategy).lower() if t else "стратегия"
        if t and t.summarize:
            strategy += " + суммаризация"
        self.chat.add_system(
            "↓ для сравнения: тот же вопрос, но в модель ушла вся история как есть — без стратегии "
            "контекста и без сжатия. Этот ответ в память не идёт."
        )
        self.chat.add_bubble(s.text, "shadow")

        row = QWidget()
        outer = QVBoxLayout(row)
        outer.setContentsMargins(6, 0, 0, 4)
        outer.setSpacing(2)
        b = s.breakdown
        cost = f" · {_money(s.cost_usd)}" if s.cost_usd is not None else ""
        outer.addWidget(_wrapped(
            f"полная история: {s.messages} сообщ. · запрос {_num(s.prompt_tokens)} т. = инструкция "
            f"{_num(b.system)} + память {_num(b.memory)} + вопрос {_num(b.question)} + схемы "
            f"{_num(b.tools)} · ответ {_num(s.completion_tokens)} т. · {s.elapsed_s} c{cost}",
            "meta",
        ))
        if t:
            diff = s.prompt_tokens - t.prompt_tokens
            share = abs(diff) / s.prompt_tokens * 100 if s.prompt_tokens else 0
            mine = config.cost_usd(reply.model, t.prompt_tokens, t.completion_tokens)
            money = (
                f" · стоимость {_money(mine)} против {_money(s.cost_usd)}"
                if mine is not None and s.cost_usd is not None else ""
            )
            verdict = _wrapped(
                f"со стратегией «{strategy}» тот же запрос весил {_num(t.prompt_tokens)} т. — на {_num(abs(diff))} т. "
                f"({share:.0f}%) {'меньше' if diff >= 0 else 'больше'}{money}",
                "meta",
            )
            verdict.setStyleSheet(
                f"color: {STRATEGY_COLORS.get(t.strategy, MUTED)}; font-size: 11px; background: transparent;"
            )
            outer.addWidget(verdict)
        if s.trimmed_pairs:
            warning = _wrapped(
                f"⚠ даже полная история не влезла в окно модели: выброшено {s.trimmed_pairs} пар(ы)", "meta"
            )
            warning.setStyleSheet(f"color: {WARN}; font-size: 11px; background: transparent;")
            outer.addWidget(warning)
        self.chat.add_row(row)

    def _show_persona_shadow(self, reply: AgentReply) -> None:
        """Ответ на тот же вопрос другим профилем — главная проверка задания.

        Контекст, память и вопрос одинаковые, отличается только профиль: разницу в
        двух ответах нечем объяснить, кроме персонализации.
        """
        s, t = reply.shadow, reply.tokens
        mine = f"«{t.persona_name}»" if t and t.persona_name else "без профиля"
        self.chat.add_system(
            f"↓ тот же вопрос, но {s.label}: память, контекст и вопрос те же — отличается "
            f"только профиль. Выше ответ для {mine}. Этот ответ в память не идёт."
        )
        self.chat.add_bubble(s.text, "shadow")

        row = QWidget()
        outer = QVBoxLayout(row)
        outer.setContentsMargins(6, 0, 0, 4)
        outer.setSpacing(2)
        b = s.breakdown
        cost = f" · {_money(s.cost_usd)}" if s.cost_usd is not None else ""
        outer.addWidget(_wrapped(
            f"{s.label}: запрос {_num(s.prompt_tokens)} т. = инструкция {_num(b.system)}"
            + (f" + профиль {_num(b.persona)}" if b.persona else " (профиля нет)")
            + f" + память {_num(b.memory)} + вопрос {_num(b.question)} · ответ "
              f"{_num(s.completion_tokens)} т. · {s.elapsed_s} c{cost}",
            "meta",
        ))
        # Второй ответ проверяем по ЕГО профилю: иначе сравнение было бы нечестным —
        # у каждого профиля свои требования к длине и формату.
        if s.checks:
            broken = [check for check in s.checks if not check.ok]
            line = _wrapped(
                (f"{s.label}: не соблюдено — "
                 + "; ".join(f"{check.label}: {check.detail}" for check in broken))
                if broken else
                f"{s.label}: соблюдён — " + ", ".join(check.detail for check in s.checks),
                "meta",
            )
            line.setStyleSheet(
                f"color: {ERR_TEXT if broken else PERSONA}; font-size: 11px; background: transparent;"
            )
            outer.addWidget(line)
        self.chat.add_row(row)

    # ------------------------------------------------------- несколько агентов --

    def _render_agents(self) -> None:
        """Перерисовать список агентов сессии (активный подсвечен)."""
        while self.agent_list.count():
            item = self.agent_list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # setParent(None) убирает карточку из окна сразу; без него старые
                # карточки живут до следующего прохода цикла событий.
                widget.setParent(None)
                widget.deleteLater()
        for agent in self.agents:
            tab = AgentItem(agent, agent is self.agent, deletable=len(self.agents) > 1)
            tab.chosen.connect(lambda a=agent: self._select_agent(a))
            tab.removed.connect(lambda a=agent: self._delete_agent(a))
            self.agent_list.addWidget(tab)
        self.agent_list.addStretch(1)

    def _edit_profile(self) -> None:
        """Поправить паспорт активного агента: имя, подпись, инструкцию."""
        dialog = ProfileDialog(self, agent=self.agent)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        self.agent.set_profile(
            name=values["name"], role=values["role"], instructions=values["instructions"]
        )
        self.setWindowTitle(f"Агент «{self.agent.profile.name}» — AI Advent")
        self._refresh()

    def _create_agent(self) -> None:
        """Завести нового агента: своё имя, своя инструкция, своя память."""
        dialog = ProfileDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        profile = AgentProfile(
            name=values["name"],
            role=values["role"],
            instructions=values["instructions"] or DEFAULT_PROFILE.instructions,
        )
        agent = Agent(profile=profile, model=values["model"], strategy=values["strategy"], store=self.store)
        agent.persist()  # новый агент попадает в историю сразу, ещё до первого вопроса
        self.agents.append(agent)
        self._select_agent(agent)

    def _select_agent(self, agent: Agent) -> None:
        """Переключиться на другого агента и показать его собственную переписку."""
        if self.busy or agent is self.agent:
            return
        self.agent = agent
        agent.mark_active()  # следующий запуск откроет разговор именно с ним
        self.setWindowTitle(f"Агент «{agent.profile.name}» — AI Advent")
        self._refresh()
        self._show_transcript()

    def _delete_agent(self, agent: Agent) -> None:
        """Удалить агента вместе с его памятью и историей (последнего удалить нельзя)."""
        if self.busy or len(self.agents) == 1:
            return
        agent.erase()
        self.agents.remove(agent)
        if agent is self.agent:
            self.agent = self.agents[-1]
            self.agent.mark_active()
            self._show_transcript()
        self._refresh()

    # ----------------------------------------------------------- ветки диалога --

    def _render_branches(self) -> None:
        """Перерисовать полосу веток активного агента (активная подсвечена)."""
        while self.branch_list.count():
            item = self.branch_list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for branch in self.agent.branches():
            chip = BranchItem(branch)
            chip.chosen.connect(lambda b=branch["id"]: self._select_branch(b))
            chip.removed.connect(lambda b=branch["id"]: self._delete_branch(b))
            self.branch_list.addWidget(chip)
        self.branch_list.addStretch(1)

    def _fork(self) -> None:
        """Создать ветку от точки ветвления (или от текущего места) и перейти в неё."""
        if self.busy:
            return
        points = self.agent.checkpoints()
        names = {b["id"]: b["name"] for b in self.agent.branches()}
        choices = [("текущее место — точка ветвления встанет здесь", None)] + [
            (f"«{c['name']}» · ветка «{names.get(c['branch'], '?')}» · после {c['messages']} сообщ. · "
             f"{_when(c['at'])}", c["id"])
            for c in reversed(points)
        ]
        dialog = NameDialog(
            self, "Новая ветка",
            "Ветка получит копию разговора до точки ветвления и дальше пойдёт своим путём: её "
            "сообщения, суммаризация, задачи и долговременная память в другие ветки не попадут. "
            "Выберите текущее место — "
            "точка встанет здесь сама; выберите прежнюю точку — ветка отойдёт от неё, и от одного "
            "места получится несколько веток. Переключаться между ветками можно в любой момент.",
            "НАЗВАНИЕ ВЕТКИ", f"Ветка {len(self.agent.branches())}",
            "например, «Веб-версия»", choices,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        chosen = dialog.chosen()
        try:
            branch = self.agent.fork(dialog.value(), chosen)
        except AgentError as e:
            self.chat.add_bubble(str(e), "error")
            return
        self.setWindowTitle(f"Агент «{self.agent.profile.name}» — AI Advent")
        self._refresh()
        self._show_transcript()
        # Точку агент ставит сам — значит, про неё надо сказать: иначе непонятно, откуда
        # в списке следующей ветки возьмётся строка «Точка 1» и что именно скопировано.
        point = (f"здесь поставлена точка ветвления «{branch['origin']}», и от неё"
                 if chosen is None else f"от прежней точки ветвления «{branch['origin']}»")
        self.chat.add_marker(
            f"⑂ {point} создана ветка «{branch['name']}»: {branch['shared']} общих сообщ. "
            f"скопированы, дальше разговор идёт в ней"
        )

    def _select_branch(self, branch: int) -> None:
        """Переключиться на ветку: лента и память — её."""
        if self.busy or branch == self.agent.branch:
            return
        try:
            self.agent.switch_branch(branch)
        except AgentError as e:
            self.chat.add_bubble(str(e), "error")
            return
        self._refresh()
        self._show_transcript()

    def _delete_branch(self, branch: int) -> None:
        """Удалить ветку вместе с её перепиской; агент вернётся в основную, если был в ней."""
        if self.busy:
            return
        was_active = branch == self.agent.branch
        try:
            self.agent.delete_branch(branch)
        except AgentError as e:
            self.chat.add_bubble(str(e), "error")
            return
        self._refresh()
        if was_active:
            self._show_transcript()

    def _show_transcript(self) -> None:
        """Перерисовать ленту перепиской агента: она хранится в нём, а не в окне."""
        self.chat.clear()
        p = self.agent.passport()
        messages = self.agent.transcript()
        if not messages:
            self.chat.add_system(
                f"Агент «{p['name']}» активен. История пуста — "
                "начните разговор или возьмите пример под полем ввода."
            )
            return

        # Метки веток: где стоят точки ветвления этой ветки и где кончается общее
        # начало, скопированное от точки другой ветки.
        markers: dict[int, list[str]] = {}
        for point in self.agent.checkpoints():
            if point["branch"] == p["branch"]:
                markers.setdefault(point["upto"], []).append(
                    f"⚑ точка ветвления «{point['name']}» — после {point['messages']} сообщ."
                )
        shared = p["branch_shared"] if p["branch"] != MAIN_BRANCH else 0
        for number, message in enumerate(messages, 1):
            self.chat.add_bubble(message["content"], "user" if message["role"] == "user" else "agent")
            for text in markers.get(message.get("id"), []):
                self.chat.add_marker(text)
            if number == shared:
                self.chat.add_marker(
                    f"⑂ выше — общее начало от точки «{p['branch_origin']}» ({shared} сообщ.), "
                    f"ниже — только ветка «{p['branch_name']}»"
                )

        when = _when(p["last_seen_at"])
        if p["restored"]:
            weight = self.agent.tokens_state()
            block = ""
            if weight["summary_active"]:
                block += (f" Начало разговора ({weight['folded_messages']} сообщ.) свёрнуто в суммаризацию "
                          f"№{weight['summary']['version']} ≈ {_num(weight['summary_tokens'])} токенов — она тоже "
                          f"поднята из базы и уйдёт в модель.")
            if weight["long_active"]:
                block += (f" Долговременная память №{weight['long']['version']} "
                          f"({weight['long']['count']} зап. ≈ {_num(weight['long_tokens'])} токенов) "
                          f"тоже поднята из базы и уйдёт в модель.")
            if weight["task_active"]:
                block += (f" Задача «{weight['task']['title']}» осталась открытой: её карточка "
                          f"({weight['task']['size']} пункт(ов) ≈ {_num(weight['task_tokens'])} "
                          f"токенов) снова в запросе — работа продолжается с того же места.")
            branch = f" Открыта ветка «{p['branch_name']}»." if p["branch"] != MAIN_BRANCH else ""
            self.chat.add_system(
                f"Разговор восстановлен из истории: {len(messages)} сообщ. ≈ "
                f"{_num(weight['history_tokens'])} токенов, последний раз говорили {when}. "
                f"Агент продолжает с того же места — в модель уйдут последние "
                f"{p['memory_messages']} сообщ. ≈ {_num(weight['breakdown']['memory'])} токенов "
                f"(глубина памяти {_plural(p['memory_turns'], 'пара', 'пары', 'пар')}, "
                f"стратегия: {p['strategy_label'].lower()}, "
                f"сжатие: {'суммаризация' if p['summarize'] else 'выключено'}).{block}{branch}"
            )
        else:
            branch = f", ветка «{p['branch_name']}»" if p["branch"] != MAIN_BRANCH else ""
            self.chat.add_system(
                f"Показана переписка агента «{p['name']}» ({len(messages)} сообщ.{branch}) — "
                "она хранится в самом агенте и пишется в историю."
            )

    def _use_example(self, text: str) -> None:
        self.input.setPlainText(text)
        self.input.setFocus()

    # ------------------------------------------------------- масштаб интерфейса --

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Wheel and event.modifiers() & Qt.ControlModifier:
            self._zoom(0.1 if event.angleDelta().y() > 0 else -0.1)
            return True
        return super().eventFilter(obj, event)

    def _zoom(self, delta: float) -> None:
        self._set_scale(self.scale + delta)

    def _set_scale(self, scale: float) -> None:
        """Применить масштаб к стилям и к размерам, заданным в коде."""
        scale = round(min(MAX_SCALE, max(MIN_SCALE, scale)), 2)
        if abs(scale - self.scale) < 0.01:
            return
        self.scale = scale
        app = QApplication.instance()
        app.setStyleSheet(build_qss(scale))
        app.setFont(QFont("Segoe UI", max(7, round(10 * scale))))

        self.sidebar.setFixedWidth(round(300 * scale))
        self.avatar.setFixedSize(round(44 * scale), round(44 * scale))
        self.input.setFixedHeight(round(56 * scale))
        self.send_btn.setFixedHeight(round(56 * scale))
        self.compare_btn.setFixedHeight(round(56 * scale))
        self.examples.setFixedHeight(round(52 * scale))
        self.agent_bar.setFixedHeight(round(52 * scale))
        self.branch_bar.setFixedHeight(round(46 * scale))
        self.state_bar.setFixedHeight(round(78 * scale))
        self.dot.setFixedSize(round(9 * scale), round(9 * scale))
        self._set_status(self._status_color, self.status.text())

        # Ширину пузырей считает сам пузырь по шрифту — после смены масштаба
        # пересчитываем, иначе текст остаётся в старой колонке.
        self.chat.bubble_limit = round(660 * scale)
        self.chat.refit()

        self.settings.setValue("ui/scale", scale)

    # --------------------------------------------------------------- действия --

    def _compare_personas(self) -> None:
        """Спросить одно и то же двумя профилями: меню прямо на кнопке.

        Отдельного окна выбора нет нарочно: профилей немного, а лишнее окно ради
        одного списка — та самая лишняя сущность.
        """
        if self.busy or not self.input.toPlainText().strip():
            self.chat.add_system("Напишите вопрос — и тогда его можно задать двумя профилями.")
            return
        menu = QMenu(self)
        for item in self.agent.personas():
            if item["id"] == self.agent.persona_id:
                continue
            menu.addAction(f"{item['name']} — {item['summary']}").setData(item["id"])
        if self.agent.persona_id:
            menu.addAction("— без профиля —").setData("")
        if menu.isEmpty():
            self.chat.add_system("Сравнивать не с чем: заведите второй профиль в панели.")
            return
        chosen = menu.exec(self.persona_btn.mapToGlobal(self.persona_btn.rect().topLeft()))
        if chosen is not None:
            self._send(compare_with=chosen.data())

    def _send(self, compare: bool = False, compare_with: str | None = None) -> None:
        text = self.input.toPlainText().strip()
        if not text or self.busy:
            return
        self.input.clear()
        self.chat.add_bubble(text, "user")
        self._pending = self.chat.add_bubble("…", "agent")

        self.busy = True
        self.send_btn.setEnabled(False)
        self.compare_btn.setEnabled(False)
        self.persona_btn.setEnabled(False)
        self._set_status(WARN, (
            "агент думает и сравнивает…" if compare else
            "агент отвечает двумя профилями…" if compare_with is not None else
            "агент думает…"
        ))
        # Пока идёт обращение, в полосе видно, на каком этапе агент сейчас работает:
        # этап — это не украшение, а то, что прямо сейчас управляет его ответом.
        task = self.agent.passport()["task"]
        if self.agent.passport()["task_active"] and task:
            self._state_text(
                progress=f"⏸ задача отложена на этапе «{task['state_label'].lower()}» — "
                f"этот вопрос идёт мимо неё"
                if task["paused"] else
                f"⏳ агент работает на этапе «{task['state_label'].lower()}» · "
                f"шаг {task['step']} из {task['total']}"
            )

        # Пока обращение идёт, кнопки выключены — двух одновременных не бывает.
        self.worker = AskWorker(self.agent, text, compare, compare_with)
        self.worker.done.connect(self._on_reply)
        self.worker.failed.connect(self._on_error)
        self.worker.progress.connect(self._on_progress)
        self.worker.start()

    def _on_progress(self, kind: str, data: dict) -> None:
        """Событие по ходу обращения: план, выполненный шаг или смена этапа.

        Обращение идёт десятки секунд, и за это время агент успевает пройти
        полплана и сменить этап. Без этого слота всё всплывало разом в конце —
        зритель видел готовый результат, но не видел движения.
        """
        if kind == "plan":
            self._state_text(progress=f"⏳ план из {len(data['steps'])} шагов составлен")
            return
        if kind == "step":
            self._state_text(progress=f"⏳ шаг {data['number']}: {data['tool']} · {data['title']}")
            return
        if kind == "state":
            task = data["task"]
            # Полосу перекрашиваем сразу: этап сменился прямо сейчас, а не «по итогам».
            self._render_states(task)
            self._state_text(progress=f"⚙ {data['moved']} · шаг {task['step']} из {task['total']}")
            self.task_line.setText(
                f"⌛ {task['title'] or task['goal']} · {task['state_label'].lower()} · "
                f"шаг {task['step']} из {task['total']}"
            )
            self.state_bar.show()
            self.task_line.show()
            self.chat.add_marker(
                f"⚙ этап задачи: {data['moved']}  ·  {config.note_source_label(data['source'])}",
                "stateMarker",
            )
            return
        if kind == "pause":
            # Отложить дело или вернуться к нему может и модель — по просьбе в
            # разговоре. Событие то же самое, что от кнопки, и метка в ленте тоже.
            task = data["task"]
            self._render_states(task)
            self.chat.add_marker(
                f"⏸ {data['moved']}  ·  {config.note_source_label(data['source'])}",
                "stateMarker",
            )

    def _on_reply(self, reply: AgentReply) -> None:
        self._pending.set_text(reply.text)
        self._show_meta(reply)
        self._show_trace(reply)
        if reply.shadow:
            self._show_shadow(reply)
        self._set_status(OK, "готов")
        self._finish()

    def _on_error(self, message: str) -> None:
        self._pending.set_role("error")
        self._pending.set_text(message)
        self._set_status(ERR_TEXT, "ошибка")
        self._finish()

    def _finish(self) -> None:
        self.busy = False
        self.send_btn.setEnabled(True)
        self.compare_btn.setEnabled(True)
        self.persona_btn.setEnabled(True)
        self._refresh()
        self.input.setFocus()

    def _on_model(self) -> None:
        if not self._loading:
            self._apply(model=self.model_box.currentData())

    # ------------------------------------------------ профиль пользователя --

    def _on_persona(self) -> None:
        """Переключить профиль: память и история при этом не меняются."""
        if self._loading:
            return
        try:
            self.agent.use_persona(self.persona_box.currentData() or "")
        except AgentError as e:
            self.chat.add_bubble(str(e), "error")
        who = self.agent.persona
        self.chat.add_system(
            f"↓ профиль пользователя: «{who.name}» — {who.summary()}. Дальше агент отвечает так."
            if who else
            "↓ профиль отключён: дальше агент отвечает без персонализации."
        )
        self._refresh()

    def _edit_persona(self) -> None:
        """Правка подключённого профиля — на месте, как и паспорт агента."""
        current = self.agent.passport()["persona"]
        if current is None:
            self.chat.add_system("Профиль не подключён — выберите его в панели или заведите новый.")
            return
        dialog = PersonaDialog(self, current)
        if not dialog.exec():
            return
        try:
            if dialog.deleted:
                self._drop_persona(current)
            else:
                self.agent.edit_persona(dialog.values())
                self._sync_personas()
        except AgentError as e:
            self.chat.add_bubble(str(e), "error")
        self._refresh()

    def _drop_persona(self, who: dict) -> None:
        """Удалить профиль и снять его со всех агентов, которые им пользовались."""
        self.agent.remove_persona(who["id"])
        for other in self.agents:
            if other is not self.agent and other.persona_id == who["id"]:
                other.use_persona("")
        self.chat.add_system(
            f"↓ профиль «{who['name']}» удалён. Агент отвечает без персонализации, "
            "пока вы не выберете другой профиль."
        )

    def _create_persona(self) -> None:
        """Завести второй профиль: с ним и сравниваются ответы «для разных людей»."""
        dialog = PersonaDialog(self)
        if not dialog.exec():
            return
        try:
            self.agent.create_persona(dialog.values())
        except AgentError as e:
            self.chat.add_bubble(str(e), "error")
        self._refresh()

    def _sync_personas(self) -> None:
        """Профиль общий, поэтому правку должны увидеть и остальные агенты.

        Каждый держит свой объект профиля, и без этого обхода агент, открытый на
        соседней вкладке, продолжил бы работать с прежним — а профиль в базе уже
        другой.
        """
        for other in self.agents:
            if other is not self.agent and other.persona_id == self.agent.persona_id:
                other.use_persona(other.persona_id)

    def _apply(self, **settings) -> None:
        """Настройки проверяет сам агент — окно только показывает результат."""
        try:
            self.agent.configure(**settings)
        except AgentError as e:
            self.chat.add_bubble(str(e), "error")
        self._refresh()  # при отказе контролы вернутся к значениям агента

    def _render_states(self, task: dict) -> None:
        """Перерисовать полосу этапов: текущий, разрешённые и запрещённые переходы."""
        while self.state_list.count():
            widget = self.state_list.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)      # иначе старые чипы живут до следующего цикла
                widget.deleteLater()
        allowed = set(task["allowed"])
        for number, state in enumerate(config.TASK_STATES):
            if number:
                arrow = QLabel("→")
                arrow.setObjectName("stateArrow")
                self.state_list.addWidget(arrow)
            self.state_list.addWidget(
                StateItem(state, state["code"] == task["state"], state["code"] in allowed,
                          bool(task.get("paused")))
            )
        # В строке — коротко, подробности в подсказке: `_clip` дописывает служебное
        # «…[ещё N символов]», и в полосе это выглядит как мусор (замечание
        # пользователя по скриншоту).
        exit_rule = config.state_exit(task["state"])
        paused = bool(task.get("paused"))
        self._state_text(
            progress=f"шаг {task['step']} из {task['total']} · {_oneline(task['current'], 45)}"
            + (f" · дальше: {exit_rule}" if exit_rule else " · это последний этап"),
            # Номер обращения и объяснение паузы — в подсказке: в строке важнее всего,
            # чьего хода ждут.
            expect=(f"⏸ пауза · ждём: {_oneline(task['expect'], 44)}" if paused else
                    f"ждём: {_oneline(task['expect_line'], 52)}"),
        )
        self.state_progress.setToolTip(
            f"Сейчас: {task['current']}\n"
            + (f"Дальше: {exit_rule}\n" if exit_rule else "Это последний этап.\n")
            + "Этапы агент проходит сам: код двигает автомат по факту работы,\n"
              "маршрутизатор — по смыслу разговора, а порядок проверяет таблица\n"
              "переходов. Переключать этапы руками не нужно."
        )
        # Третья величина состояния — ожидаемое действие. На паузе она же объясняет,
        # почему ничего не происходит: ход за человеком, и до его слова автомат замер.
        self.state_expect.setToolTip(
            (f"Ждём: {task['expect_line']}\n\n"
             f"Задача отложена на обращении {task['paused_turn']}: карточка ушла из запроса,\n"
             "вместо неё едет закладка в одну строку, а любой переход по этапам\n"
             "отклоняется — хоть от модели, хоть от кнопки, пока работу не продолжат."
             if paused else
             f"Ждём: {task['expect_line']}\n\n"
             "Ожидаемое действие: что должно произойти дальше и от кого этого ждут.\n"
             "Считает код по этапу и шагу, маршрутизатор может уточнить формулировку.")
        )
        self.pause_btn.setText("Продолжить" if paused else "Пауза")
        self.pause_btn.setToolTip(
            "Вернуться к отложенной задаче: карточка снова уйдёт в запрос целиком,\n"
            "и агент продолжит с того же шага, не переспрашивая условий."
            if paused else
            "Отложить задачу на любом этапе. Этап и шаги сохранятся, автомат замрёт,\n"
            "а карточка перестанет занимать контекст — останется закладка в строку."
        )

    def _state_text(self, progress: str | None = None, expect: str | None = None) -> None:
        """Записать строки полосы этапов, вписав их в фактическую ширину.

        Места здесь мало: чипы четырёх этапов и две кнопки съедают ширину. Подписи
        вписываются в свою долю сами (`ElidedLabel`), а подробности остаются в
        подсказке — обрезанная строка никогда не уносит с собой смысл целиком.
        """
        if progress is not None:
            self.state_progress.setFullText(progress)
        if expect is not None:
            self.state_expect.setFullText(expect)

    def _toggle_pause(self) -> None:
        """Отложить задачу или вернуться к ней — второе действие человека в автомате.

        Первое — «Завершить задачу». Больше кнопок у автомата нет: этапы он проходит
        сам, и кликать по ним человеку не нужно. Пауза — исключение не потому, что
        человек привилегирован, а потому, что «отложим до завтра» знает только он;
        модель то же самое делает просьбой, и проходит она тем же кодом.
        """
        try:
            task = self.agent.passport()["task"] or {}
            result = (self.agent.resume_task() if task.get("paused")
                      else self.agent.pause_task())
        except AgentError as e:
            self.chat.add_bubble(str(e), "error")
            return
        self.chat.add_marker(f"⏸ {result['moved']}  ·  решение человека", "stateMarker")
        self._refresh()

    def _open_workspace(self) -> None:
        """Показать песочницу агента в проводнике: там лежит всё, что он записал."""
        folder = Path(self.agent.passport()["workspace"])
        folder.mkdir(parents=True, exist_ok=True)   # до первого файла папки может не быть
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _close_task(self) -> None:
        """Единственное действие человека в автомате: «всё, дело закрыто».

        Этапы агент проходит сам, поэтому переключать их руками незачем — но
        поставить точку человек должен уметь. Кнопка прокручивает оставшиеся этапы
        по порядку (не перепрыгивая) и показывает пройденный путь.
        """
        try:
            result = self.agent.close_task()
        except AgentError as e:
            self.chat.add_bubble(str(e), "error")
            return
        card = result["task"]
        path = " → ".join(result["moved"]) if result["moved"] else "этап уже был последним"
        self.chat.add_system(
            f"⚙ задача «{card['title']}» завершена ({path}). Карточка ушла в архив и больше не "
            f"занимает контекст, а её итог остался записью в долговременной памяти."
        )
        self._refresh()

    def _reset(self) -> None:
        self.agent.reset()
        self.chat.clear()
        self.chat.add_system(
            "Все три слоя памяти очищены, история на диске стёрта — агент снова не знает, "
            "о чём был разговор, над чем работал и что о вас знал, и не вспомнит этого "
            "после перезапуска."
        )
        self._refresh()

    def apply_saved_scale(self) -> None:
        """Применить масштаб, сохранённый с прошлого запуска."""
        saved, self.scale = self.scale, 1.0
        self._set_scale(saved)

    def closeEvent(self, event) -> None:
        # Даём текущему обращению завершиться, чтобы поток не умер на полуслове.
        if self.worker is not None and self.worker.isRunning():
            self.worker.wait(3000)
        super().closeEvent(event)


def _trace_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("traceLabel")
    return label


def _wrapped(text: str, name: str) -> QLabel:
    """Метка с переносом, которая честно сообщает layout свою высоту.

    QLabel с `setWordWrap` считает высоту по своему sizeHint и, если строка
    занимает больше строк, чем он ожидал, накладывается на соседей. Лечится это
    политикой размера с heightForWidth: тогда layout спрашивает высоту под ту
    ширину, которая досталась метке на самом деле.
    """
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(True)
    policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    policy.setHeightForWidth(True)
    label.setSizePolicy(policy)
    return label


def _history_table(agent: Agent, history: list[dict]) -> QPlainTextEdit:
    """Сообщения, суммаризации, записи, задачи, ветки и точки так, как они лежат в базе.

    Базу не откроешь блокнотом, поэтому её содержимое показываем прямо в окне. Здесь
    же видно главное свойство модели памяти: слои действительно хранятся отдельно —
    краткосрочная в `messages` и `summaries`, рабочая в `tasks`, долговременная в
    `notes`, и записи одного слоя ничего не меняют в другом. Ветки — тоже строки:
    у каждой своя копия общего начала.
    """
    agent_id, branch = agent.id, agent.branch
    lines = [
        "sqlite> SELECT id, branch, role, at, content FROM messages",
        f"        WHERE agent_id = '{agent_id}' AND branch = {branch} ORDER BY id;",
        "",
        f"{'id':>5}  {'ветка':>5}  {'role':<9}  {'at':<15}  content",
        f"{'-' * 5}  {'-' * 5}  {'-' * 9}  {'-' * 15}  {'-' * 40}",
    ]
    for message in history:
        when = time.strftime("%d.%m %H:%M:%S", time.localtime(message.get("at") or 0))
        lines.append(f"{message.get('id', '—'):>5}  {branch:>5}  {message['role']:<9}  {when:<15}  "
                     f"{_oneline(message.get('content'))}")
    if not history:
        lines.append("-- строк нет")

    summaries = agent.summaries()
    lines += [
        "",
        "sqlite> SELECT version, turn, upto, folded_messages, folded_tokens, summary_tokens, content",
        f"        FROM summaries WHERE agent_id = '{agent_id}' AND branch = {branch} ORDER BY id;",
        "",
        f"{'вер.':>4}  {'обр.':>4}  {'upto':>5}  {'сообщ.':>6}  {'весили':>7}  {'весит':>6}  content",
        f"{'-' * 4}  {'-' * 4}  {'-' * 5}  {'-' * 6}  {'-' * 7}  {'-' * 6}  {'-' * 40}",
    ]
    for row in summaries:
        lines.append(
            f"{row['version']:>4}  {row['turn']:>4}  {row['upto']:>5}  {row['folded_messages']:>6}  "
            f"{row['folded_tokens']:>7}  {row['summary_tokens']:>6}  {_oneline(row.get('content'))}"
        )
    if not summaries:
        lines.append("-- строк нет: суммаризация ещё не составлялась")
    else:
        lines += [
            "",
            "-- «upto» — id последнего сообщения, вошедшего в суммаризацию; «сообщ.» — сколько она",
            "-- заменяет всего, «весили» — сколько они стоили бы в запросе, «весит» — сама суммаризация.",
        ]

    notes = agent.notes()
    lines += [
        "",
        "sqlite> SELECT kind, key, value, source, turn, upto FROM notes",
        f"        WHERE agent_id = '{agent_id}' AND branch = {branch} ORDER BY id;",
        "",
        f"{'kind':<10}  {'source':<8}  {'обр.':>4}  {'upto':>5}  key: value",
        f"{'-' * 10}  {'-' * 8}  {'-' * 4}  {'-' * 5}  {'-' * 40}",
    ]
    upto = agent.passport()["long"]["upto"]
    for row in notes:
        pair = "{}: {}".format(row["key"], row["value"])
        lines.append(
            f"{row['kind']:<10}  {row['source']:<8}  {row['turn']:>4}  {upto:>5}  {_oneline(pair)}"
        )
    if not notes:
        lines.append("-- строк нет: долговременная память пуста")
    else:
        lines += ["", "-- ДОЛГОВРЕМЕННАЯ ПАМЯТЬ: по строке на запись. «kind» — вид (профиль, решение,",
                  "-- знание), «source» — кто её положил (правило, маршрутизатор, инструмент, из",
                  "-- задачи), «upto» — до какого сообщения слой уже разобран."]

    tasks = agent.tasks()
    lines += [
        "",
        "sqlite> SELECT id, status, state, paused, expect, title, turn, closed_turn, steps",
        f"        FROM tasks WHERE agent_id = '{agent_id}' AND branch = {branch} ORDER BY id;",
        "",
        f"{'id':>4}  {'status':<7}  {'state':<11}  {'пауза':<6}  {'title':<20}  пункты",
        f"{'-' * 4}  {'-' * 7}  {'-' * 11}  {'-' * 6}  {'-' * 20}  {'-' * 40}",
    ]
    for row in tasks:
        items = (f"шагов {len(row['steps'])}, находок {len(row['findings'])}, "
                 f"файлов {len(row['artifacts'])}, вопросов {len(row['questions'])}")
        lines.append(f"{row['id']:>4}  {row['status']:<7}  {row['state']:<11}  "
                     f"{('да' if row['paused'] else 'нет'):<6}  "
                     f"{_oneline(row['title'], 20):<20}  {items}")
        if row["open"]:
            lines.append(f"      обращение {row['turn']}, ждём: {row['expect_line']}")
    if not tasks:
        lines.append("-- строк нет: задач не было")
    else:
        lines += ["", "-- РАБОЧАЯ ПАМЯТЬ: по строке на задачу. В запрос уходит только та, у которой",
                  "-- status = open; закрытые остаются архивом и контекст не занимают. Состояние",
                  "-- задачи лежит колонками: state — этап автомата, expect/expect_who — ожидаемое",
                  "-- действие, paused — отложена ли она (этап при этом сохраняется)."]

    people = agent.personas()
    active = agent.passport()["persona_id"]
    lines += [
        "",
        "sqlite> SELECT id, name, style, format, length, limits, prefs FROM personas ORDER BY rowid;",
        "",
        f"{'id':<10}  {'name':<16}  {'style':<9}  {'format':<8}  {'length':<7}  огранич./предпочт.",
        f"{'-' * 10}  {'-' * 16}  {'-' * 9}  {'-' * 8}  {'-' * 7}  {'-' * 30}",
    ]
    for row in people:
        mark = " ←" if row["id"] == active else ""
        lines.append(
            f"{row['id']:<10}  {_oneline(row['name'], 16):<16}  {row['style']:<9}  "
            f"{row['format']:<8}  {row['length']:<7}  "
            f"{len(row['limits'])} / {len(row['prefs'])}{mark}"
        )
    if not people:
        lines.append("-- строк нет: профилей ещё не заводили")
    else:
        lines += ["", "-- ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ: единственная таблица без agent_id и branch — профиль",
                  "-- описывает человека, а не разговор, поэтому переживает и смену агента, и",
                  "-- «забыть разговор», и его можно отдать нескольким агентам. Стрелкой отмечен тот,",
                  "-- что подключён к запросам этого агента (ссылка лежит в agents.persona)."]

    branches = agent.branches()
    lines += [
        "",
        "sqlite> SELECT id, name, origin, shared, at FROM branches",
        f"        WHERE agent_id = '{agent_id}' ORDER BY id;",
        "",
        f"{'id':>4}  {'name':<22}  {'origin':<22}  {'общих':>5}  {'сообщ.':>6}  at",
        f"{'-' * 4}  {'-' * 22}  {'-' * 22}  {'-' * 5}  {'-' * 6}  {'-' * 14}",
    ]
    for row in branches[1:]:
        lines.append(
            f"{row['id']:>4}  {row['name'][:22]:<22}  {row['origin'][:22]:<22}  {row['shared']:>5}  "
            f"{row['messages']:>6}  {time.strftime('%d.%m %H:%M:%S', time.localtime(row.get('at') or 0))}"
        )
    if len(branches) == 1:
        lines.append("-- строк нет: основная ветка (branch = 0) строки не имеет, других веток нет")
    else:
        lines += ["", "-- основная ветка — это branch = 0 без строки; у остальных «общих» — сколько сообщений",
                  "-- скопировано от точки, а дальше у каждой ветки своя переписка."]

    points = agent.checkpoints()
    lines += [
        "",
        "sqlite> SELECT id, branch, upto, messages, name FROM checkpoints",
        f"        WHERE agent_id = '{agent_id}' ORDER BY id;",
        "",
        f"{'id':>4}  {'ветка':>5}  {'upto':>5}  {'сообщ.':>6}  name",
        f"{'-' * 4}  {'-' * 5}  {'-' * 5}  {'-' * 6}  {'-' * 24}",
    ]
    for row in points:
        lines.append(f"{row['id']:>4}  {row['branch']:>5}  {row['upto']:>5}  {row['messages']:>6}  {row['name']}")
    if not points:
        lines.append("-- строк нет: точек ветвления нет")

    view = QPlainTextEdit("\n".join(lines))
    view.setObjectName("raw")
    view.setReadOnly(True)
    view.setLineWrapMode(QPlainTextEdit.NoWrap)
    return view


def _oneline(text: str | None, limit: int = 96) -> str:
    """Текст поля одной строкой для таблицы «В базе»."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _layers_page(state: dict, tasks: list[dict]) -> QWidget:
    """Вкладка «Слои»: что лежит в каждом слое прямо сейчас и кто это туда положил.

    Это ответ на вопрос задания «какие данные попадают в каждый слой» — не описание
    модели, а её содержимое: карта сверху, под ней долговременная память записями с
    видом и источником, карточка открытой задачи и архив закрытых.
    """
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 10, 0, 0)
    lay.setSpacing(8)

    head = ["Модель памяти: три слоя, каждый со своим хранением и своим сроком жизни."]
    for layer in state["layers"]:
        mark = "включён" if layer["active"] else "выключен"
        head.append(
            f"• {layer['label']} ({layer['en']}) — {layer['what']}; хранение: {layer['store']}; "
            f"{mark}, {_num(layer['tokens'])} т. в следующем запросе; {layer['state']}."
        )
    note = QLabel("\n".join(head))
    note.setObjectName("note")
    note.setWordWrap(True)
    lay.addWidget(note)

    # Профиль идёт первым и отдельно от слоёв: он лежит в своей таблице, не
    # принадлежит ветке и отвечает не на «что агент помнит», а на «для кого он
    # говорит». Смешать его со слоями значило бы сделать вид, что слоёв четыре.
    who = state["persona"]
    lines = ["ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ — персонализация поверх памяти (таблица personas, вне веток)"]
    if who:
        lines.append(f"профиль «{who['name']}» — {who['summary']}")
        lines.append(f"вес в запросе: {_num(state['persona_tokens'])} т., и так в КАЖДОМ обращении")
        lines += ["  " + line for line in who["text"].splitlines()]
        if who["prefs"]:
            lines.append("  источники значений:")
            lines += [f"    {item['key']}: {item['value']} "
                      f"({config.persona_source_label(item['source'])}"
                      + (f", обращение {item['turn']}" if item["turn"] else "") + ")"
                      for item in who["prefs"]]
        checked = [config.limit_label(code) for code in who["checked"]]
        asked = [code for code in who["limits"] if code not in who["checked"]]
        lines.append(f"  проверяется кодом после ответа: длина, формат"
                     + (f", {', '.join(checked)}" if checked else ""))
        if asked:
            lines.append(f"  только просьба в промпте: {'; '.join(asked)}")
    else:
        lines.append("-- профиль не подключён: форму ответа выбирает модель")

    lines += ["", "ДОЛГОВРЕМЕННАЯ ПАМЯТЬ — профиль, решения, знания, инварианты",
              f"{'вид':<12} {'источник':<14} {'обр.':>5}  ключ: значение",
              f"{'-' * 12} {'-' * 14} {'-' * 5}  {'-' * 40}"]
    notes = state["long"]["notes"]
    for item in notes:
        mark = ""
        if item["kind"] == "invariant":
            bans = memory.invariant_bans(item["value"])
            mark = f"   [проверяется кодом: {', '.join(bans)}]" if bans else "   [только просьба в промпте]"
        lines.append(f"{config.note_kind_label(item['kind']):<12} "
                     f"{config.note_source_label(item['source']):<14} {item['turn']:>5}  "
                     f"{item['key']}: {item['value']}{mark}")
    if not notes:
        lines.append("-- записей нет: слой пуст"
                     + ("" if state["long_active"] else " или выключен (стратегия «Факты»)"))
    else:
        lines += ["", "-- инвариант со словом «запрещено: X, Y» агент сверяет со своим ответом после",
                  "-- генерации; без этого маркера правило остаётся просьбой в инструкции."]

    lines += ["", "РАБОЧАЯ ПАМЯТЬ — конечный автомат текущей задачи"]
    if not state["working"]:
        lines.append("-- слой выключен: тумблер «Рабочая память» в панели")
    open_task = next((t for t in tasks if t["open"]), None)
    if open_task:
        allowed = ", ".join(config.state_label(code) for code in open_task["allowed"]) or "никуда"
        lines.append(f"открытая задача №{open_task['id']} · заведена на обращении {open_task['turn']}")
        # Состояние задачи — три величины из задания, и каждая своей строкой: этап,
        # текущий шаг, ожидаемое действие.
        lines.append(f"  этап: {open_task['state_label']} ({open_task['state_en']}) · "
                     f"шаг {open_task['step']} из {open_task['total']} · "
                     f"сейчас: {open_task['current']}")
        lines.append(f"  ожидаемое действие: {open_task['expect_line']}")
        if open_task["paused"]:
            lines.append(f"  ПАУЗА с обращения {open_task['paused_turn']}: автомат заморожен, "
                         f"переходы отклоняются, в запрос уходит только закладка —")
            lines.append(f"    {open_task['bookmark']}")
        lines.append(f"  разрешённые переходы: {allowed} — остальные код отклонит"
                     + (" (и все, пока стоит пауза)" if open_task["paused"] else ""))
        lines.append(f"  в запрос на этом этапе уходит: "
                     + ("(ничего, задача на паузе)" if open_task["paused"] else
                        ', '.join(config.state_sections(open_task['state'])) or '(ничего)'))
        lines += ["  " + line for line in open_task["text"].splitlines()]
    else:
        lines.append("-- открытой задачи нет: в запрос ничего не уходит")
    closed = [t for t in tasks if not t["open"]]
    if closed:
        lines += ["", f"архив завершённых задач ({len(closed)}) — в запрос не уходят:"]
        lines += [f"  №{t['id']} «{t['title']}» — {t['summary']} (этап {t['state_label'].lower()}, "
                  f"обращение {t['closed_turn']})" for t in closed]

    view = QPlainTextEdit("\n".join(lines))
    view.setObjectName("raw")
    view.setReadOnly(True)
    view.setLineWrapMode(QPlainTextEdit.NoWrap)
    lay.addWidget(view, 1)
    return page


def _verdict(t) -> str:
    """Итог сравнения с полной историей: сбережено или пока дороже, и на сколько."""
    share = abs(t.saved) / t.uncompressed * 100 if t.uncompressed else 0
    return (
        f"сбережено {_num(t.saved)} т. ({share:.0f}%)" if t.saved >= 0 else
        f"пока на {_num(-t.saved)} т. ({share:.0f}%) дороже — окупается на длине разговора"
    )


def _strategy_line(t, branch_name: str) -> str:
    """Строка под ответом про цену стратегии: что осталось за окном и чем это заменено.

    Про сжатие тут ничего нет — оно опция поверх стратегии, и у него своя строка. Со
    включённым сжатием выпавшее из окна не отброшено, а ждёт суммаризации, поэтому и
    говорит о нём соседняя строка, а не эта.
    """
    beyond = f"за окном {t.dropped_messages} сообщ. ≈ {_num(t.dropped_tokens)} т."
    # Сравнение с полной историей — только без сжатия: со сжатием оно стоит в строке
    # про него, и повторять то же самое дважды незачем.
    full = (f" · с полной историей запрос был бы ≈ {_num(t.uncompressed)} т.: {_verdict(t)}"
            if t.dropped_messages and not t.summarize else "")
    if t.strategy == "facts":
        if t.long_version:
            replaced = (f" · {beyond} — их заменяет долговременная память"
                        if t.dropped_messages and not t.summarize else "")
            return (f"долговременная память: версия №{t.long_version} ({t.long_items} зап.) весит "
                    f"{_num(t.breakdown.long)} т.{replaced}{full}")
        return (f"долговременная память: пока пуста · {beyond} в запрос не ушли"
                if t.dropped_messages else "")
    if t.strategy == "branches":
        if t.summarize:
            return f"ветка «{branch_name}»: у неё своё окно памяти и своя суммаризация"
        if not t.dropped_messages:
            return f"ветка «{branch_name}»: вся её история уместилась в окно памяти"
        return f"ветка «{branch_name}»: {beyond}{full}"
    if t.dropped_messages and not t.summarize:
        return (f"скользящее окно: {beyond} отброшены — подробности оттуда модели не видны{full}")
    return ""


def _states_path(moved: list) -> str:
    """Путь по этапам одной строкой: «Планирование → Выполнение → Проверка».

    Переходы приходят по одному («A → B», «B → C»), и простая склейка повторяла
    средний этап дважды. Здесь звенья сшиваются встык.
    """
    path: list[str] = []
    for move in moved:
        parts = [p.strip() for p in move.split("→")]
        for part in parts:
            if not path or path[-1] != part:
                path.append(part)
    return " → ".join(path)


def _layers_line(t) -> str:
    """Строка под ответом про рабочую память: что за задача и во что она обходится.

    Про долговременную память говорит строка стратегии, про краткосрочную — строка
    разбивки: у каждого слоя своё место, и повторять одно и то же трижды незачем.
    """
    if not t.working:
        return ""
    if t.task_paused:
        return (f"рабочая память: задача «{t.task_title}» отложена на этапе "
                f"«{config.state_label(t.task_state)}» — карточка из запроса ушла, осталась "
                f"закладка в строку ({_num(t.breakdown.task)} т.); ждём: {t.task_expect}")
    if t.task_items:
        return (f"рабочая память: задача «{t.task_title}» — этап «{config.state_label(t.task_state)}», "
                f"шаг {t.task_step} из {t.task_total}; в запрос ушли только части карточки, нужные "
                f"этому этапу — {_num(t.breakdown.task)} т."
                + (f"; ждём: {t.task_expect}" if t.task_expect else "")
                + ("; это первый ответ после паузы — карточка вернулась целиком"
                   if t.task_resumed else ""))
    return "рабочая память: задачи нет — слой в запрос ничего не добавил"


def _persona_line(t, update) -> str:
    """Строка под ответом про персонализацию: чей профиль ушёл и соблюдён ли он.

    Соблюдение показываем всегда, а не только при нарушении: иначе «агент учитывает
    профиль» останется словами — а так под каждым ответом видно, что именно
    проверено и с каким результатом.
    """
    if not t.persona_name:
        return "профиль пользователя не подключён: форму ответа выбирает модель"
    head = (f"профиль «{t.persona_name}» — {t.persona_summary} · "
            f"вес в запросе: {_num(t.breakdown.persona)} т.")
    if update is None or not update.checks:
        return head
    if update.broken:
        return head + f" · соблюдено: {len(update.checks) - len(update.broken)} из {len(update.checks)}"
    return head + " · соблюдён полностью: " + ", ".join(check.detail for check in update.checks)


def _compression_line(t) -> str:
    """Строка под ответом про сжатие истории: что свёрнуто и во сколько это обошлось."""
    if not t.summarize:
        return ""
    if t.folded_messages:
        waiting = f" · ждут суммаризации: {t.pending_messages} сообщ." if t.pending_messages else ""
        return (f"сжатие: суммаризация №{t.summary_version} вместо {t.folded_messages} сообщ. "
                f"≈ {_num(t.folded_tokens)} т. весит {_num(t.breakdown.summary)} т. · с полной историей "
                f"запрос был бы ≈ {_num(t.uncompressed)} т.: {_verdict(t)}{waiting}")
    if t.pending_messages:
        return (f"сжатие: за окном памяти {t.pending_messages} сообщ. ≈ {_num(t.pending_tokens)} т. "
                f"ждут суммаризации — в этот запрос они не ушли")
    return ""


def _strategies_table(agents: list[Agent]) -> QPlainTextEdit:
    """Сводка по агентам окна: стратегия и расход рядом, чтобы сравнить один сценарий на разных стратегиях."""
    lines = [
        f"{'агент':<16} {'стратегия':<12} {'обр.':>5} {'выз.':>5} {'запрос ср.':>10} {'ответы ср.':>10} "
        f"{'всего т.':>9} {'стоимость':>10} {'за окном':>8} {'заменено':<12}",
        f"{'-' * 16} {'-' * 12} {'-' * 5} {'-' * 5} {'-' * 10} {'-' * 10} {'-' * 9} {'-' * 10} {'-' * 8} {'-' * 12}",
    ]
    for agent in agents:
        rows = agent.usage_log()
        name = agent.profile.name[:16]
        strategy = config.strategy_short(agent.strategy) + ("+сум" if agent.summarize else "")
        if not rows:
            lines.append(f"{name:<16} {strategy:<12} {0:>5} {0:>5} {'—':>10} {'—':>10} {0:>9} {'—':>10} {'—':>8} {'—':<12}")
            continue
        if len({row.get("strategy") or "" for row in rows}) > 1:
            strategy += "*"   # стратегию меняли по ходу разговора
        turns = len(rows)
        last = rows[-1]
        folded = last.get("summary_tokens")
        sticky = last.get("long_tokens") or last.get("facts_tokens")
        replaced = ("сумм.+долгоср." if folded and sticky else
                    "суммаризация" if folded else "долговременная" if sticky else "—")
        lines.append(
            f"{name:<16} {strategy[:12]:<12} {turns:>5} {sum(r['llm_calls'] for r in rows):>5} "
            f"{round(sum(r['context_tokens'] for r in rows) / turns):>10} "
            f"{round(sum(r['completion_tokens'] for r in rows) / turns):>10} "
            f"{sum(r['total_tokens'] for r in rows):>9} {_money(sum(r['cost_usd'] or 0.0 for r in rows)):>10} "
            f"{last.get('dropped_messages') or 0:>8} {replaced:<12}"
        )
    lines += [
        "",
        "-- «запрос ср.» — средний вес контекста первого запроса обращения, «ответы ср.» — токены всех",
        "-- ответов за обращение (с вызовами стратегии и сжатия), «за окном» — сколько сообщений истории",
        "-- не ушло в модель дословно в последнем обращении, «заменено» — чем их представили.",
        "-- «+сум» — поверх стратегии включено сжатие истории. Звёздочка — стратегию меняли по ходу",
        "-- разговора, строки расхода смешанные.",
    ]
    view = QPlainTextEdit("\n".join(lines))
    view.setObjectName("raw")
    view.setReadOnly(True)
    view.setLineWrapMode(QPlainTextEdit.NoWrap)
    return view


def _summary_page(state: dict) -> QWidget:
    """Вкладка «Суммаризация»: сам текст, что она заменяет и когда обновится."""
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 10, 0, 0)
    lay.setSpacing(8)

    summary = state["summary"]
    if not state["summarize"]:
        text = ("Сжатие выключено: суммаризация не составляется и в запрос не уходит. "
                + (f"Суммаризация №{summary['version']} ({summary['messages']} сообщ.) сохранена и вернётся "
                   f"в запрос, как только включить сжатие истории." if summary["version"] else ""))
    elif not summary["version"]:
        text = (f"Суммаризации пока нет. Она появится, когда за окном памяти накопится "
                f"{state['summary_every']} сообщ.: сейчас там {state['pending_messages']}.")
    else:
        text = (
            f"Суммаризация №{summary['version']} · вместо {summary['messages']} сообщ. ≈ "
            f"{_num(summary['tokens'])} т. · сама весит ≈ {_num(state['summary_tokens'])} т. вместе с "
            f"заметкой, которая объясняет модели, что это её память · обновлена {_when(summary['at'])} · "
            f"следующее обновление, когда за окном накопится {state['summary_every']} сообщ. "
            f"(сейчас {state['pending_messages']})"
        )
    note = QLabel(text)
    note.setObjectName("note")
    note.setWordWrap(True)
    lay.addWidget(note)

    view = QPlainTextEdit(summary["text"] or "— суммаризация пуста —")
    view.setObjectName("raw")
    view.setReadOnly(True)
    lay.addWidget(view, 1)
    return page


def _strategy_text(state: dict, rows: list[dict]) -> str:
    """Стратегия словами: что за окном, чем заменено, сколько сберегла сейчас и за всё время."""
    strategy = state["strategy"]
    before, after = state["uncompressed"], state["context_tokens"]
    share = abs(before - after) / before * 100 if before else 0
    dropped_total = sum(row.get("dropped_tokens") or 0 for row in rows)
    beyond = f"за окном памяти {state['dropped_messages']} сообщ. ≈ {_num(state['dropped_tokens'])} токенов"
    if strategy == "window":
        if state["summarize"]:
            return ("Скользящее окно: дословно в модель уходят последние пары «вопрос-ответ». Выпавшее "
                    "из окна при этом не теряется — включено сжатие, и оно уходит в запрос суммаризацией "
                    "(о ней — раздел ниже). Выключите сжатие, и начало разговора будет отбрасываться: "
                    "это и есть чистая стратегия скользящего окна.")
        if not state["dropped_messages"]:
            return ("Скользящее окно: пока вся история умещается в окно памяти, отбрасывать нечего — "
                    "стратегия ещё не сработала. Уменьшите глубину памяти или продолжите разговор.")
        return (
            f"Скользящее окно: {beyond}, в запрос они не идут вовсе. Следующий запрос ≈{_num(after)} токенов, "
            f"с полной историей он весил бы ≈{_num(before)} — дешевле на {share:.0f}%. Цена — начало разговора "
            f"агент не помнит: важная деталь оттуда для модели потеряна. За все обращения отброшено "
            f"≈{_num(dropped_total)} токенов сообщений."
        )
    if strategy == "facts":
        long = state["long"]
        if not long["count"]:
            return ("Долговременная память пока пуста: записи появятся после первого ответа. Дальше "
                    "после каждого ответа маршрутизатор раскладывает новое по видам — профиль, решения, "
                    "знания, — и блок уходит в запрос вместе с окном памяти.")
        now = (
            f"дешевле на {share:.0f}%" if before >= after else
            f"пока дороже на {share:.0f}%: блок вместе с заметкой тяжелее тех нескольких сообщений, "
            f"что за окном, и окупается на длине разговора"
        )
        rest = (
            "Сообщения за окном памяти представлены сразу и записями, и суммаризацией: сжатие включено "
            "поверх стратегии (о нём — раздел ниже)."
            if state["summarize"] else
            f"{beyond[0].upper() + beyond[1:]} — их представляет долговременная память, а не сами сообщения."
        )
        kinds = ", ".join(f"{config.note_kind_label(kind)} {len(items)}"
                          for kind, items in long["by_kind"].items())
        return (
            f"Долговременная память №{long['version']}: {long['count']} зап. ({kinds}), блок весит "
            f"≈{_num(state['long_tokens'])} токенов вместе с заметкой, которая объясняет модели, что это "
            f"её память. {rest} Следующий запрос ≈{_num(after)} токенов, с полной "
            f"историей ≈{_num(before)} — {now}. Цена механизма — вызов маршрутизатора после каждого ответа; "
            f"его токены входят в расход обращения (колонка «выз.» в таблице)."
        )
    if strategy == "branches":
        shared = f", из них общих с точкой «{state['branch_origin']}» — {state['branch_shared']}" \
            if state["branch"] != MAIN_BRANCH else ""
        rest = ("выпавшее из окна сворачивается в суммаризацию этой же ветки — сжатие включено"
                if state["summarize"] else beyond)
        return (
            f"Ветки диалога: активна «{state['branch_name']}» — {state['history_messages']} сообщ.{shared}. "
            f"В модель уходит окно памяти этой ветки; {rest}. Ветвление не сжимает историю, а разрезает "
            f"её: каждая ветка короче общего разговора, и окно памяти чаще покрывает её целиком, а "
            f"альтернативы не засоряют контекст друг друга. Следующий запрос ≈{_num(after)} токенов, с полной "
            f"историей ветки ≈{_num(before)}."
        )
    return f"Стратегия «{state['strategy_label']}» неизвестна этой версии интерфейса."


def _persona_text(state: dict, rows: list[dict]) -> str:
    """Раздел окна токенов: во что обходится персонализация и что в ней проверяется."""
    who = state["persona"]
    if not who:
        return (
            "Профиль пользователя не подключён: стиль, формат и длину ответа модель выбирает "
            "сама, и проверять тут нечего. Подключите профиль в панели — форма ответа станет "
            "вашей, а её соблюдение начнёт проверяться после каждого ответа."
        )
    spent = sum(row.get("persona_tokens") or 0 for row in rows)
    turns = sum(1 for row in rows if row.get("persona_tokens"))
    checked = [config.limit_label(code) for code in who["checked"]]
    asked = [code for code in who["limits"] if code not in who["checked"]]
    return (
        f"Профиль «{who['name']}» — {who['summary']} — весит {_num(state['persona_tokens'])} токенов и "
        f"уходит в КАЖДЫЙ запрос — за {turns} обращен(ий) это {_num(spent)} токенов. В отличие от "
        "памяти, эта цена не растёт с разговором: профиль не накапливается.\n"
        f"После ответа код сверяет с профилем длину (≈{who['length_words']} слов), формат "
        f"«{who['format_label'].lower()}»"
        + (f" и ограничения: {', '.join(checked)}" if checked else "")
        + ". Расхождение видно строкой под ответом; ответ при этом не перегенерируется — решать вам."
        + (f"\nОстальное уходит просьбой в инструкцию и кодом не проверяется: {'; '.join(asked)}."
           if asked else "")
    )


def _compression_text(state: dict, rows: list[dict]) -> str:
    """Сжатие словами: что свёрнуто, во сколько обошлось сейчас и за всё время.

    Отдельно от стратегии, потому что сжатие — опция поверх любой из них: его цена
    складывается со стратегией, а не заменяет её.
    """
    before, after = state["uncompressed"], state["context_tokens"]
    share = abs(before - after) / before * 100 if before else 0
    summary = state["summary"]
    if not state["summarize"]:
        return ("Сжатие выключено: то, что выпало из окна памяти, в запрос не возвращается. "
                + (f"Суммаризация №{summary['version']} ({summary['messages']} сообщ.) сохранена в базе и "
                   f"вернётся в запрос, как только включить сжатие." if summary["version"] else
                   "Включите «Суммаризировать старую историю» в панели — и начало разговора будет "
                   "уходить в модель коротким списком фактов вместо самих сообщений."))
    if not summary["version"]:
        return (f"Суммаризации пока нет: за окном памяти {state['pending_messages']} сообщ., она составится, "
                f"когда их наберётся {state['summary_every']}. Пока разговор короче окна, сжимать нечего.")
    saved_total = sum((row.get("folded_tokens") or 0) - (row.get("summary_tokens") or 0) for row in rows)
    now = (
        f"дешевле на {share:.0f}%" if before >= after else
        f"пока дороже на {share:.0f}%: суммаризация вместе с заметкой тяжелее тех нескольких сообщений, "
        f"что заменила, и окупается на длине разговора"
    )
    total = (
        f"За все обращения суммаризация сберегла ≈{_num(saved_total)} токенов" if saved_total >= 0 else
        f"За все обращения суммаризация пока обошлась на ≈{_num(-saved_total)} токенов дороже сообщений"
    )
    return (
        f"Суммаризация №{summary['version']} заменяет {summary['messages']} сообщ. (≈{_num(summary['tokens'])} "
        f"токенов) и весит ≈{_num(state['summary_tokens'])} — вместе с заметкой, которая объясняет "
        f"модели, что это её память. Следующий запрос ≈{_num(after)} токенов, с полной историей как есть "
        f"он весил бы ≈{_num(before)} — {now}. {total}: сумма по строкам расхода, что весили бы "
        f"свёрнутые сообщения минус вес суммаризации. Само обновление суммаризации — отдельный вызов "
        f"модели, его токены входят в расход того обращения, после которого он случился."
    )


def _usage_table(agent_id: str, rows: list[dict]) -> QPlainTextEdit:
    """Расход так, как он лежит в базе: строка таблицы `usage` — строка текста."""
    lines = [
        "sqlite> SELECT turn, strategy, llm_calls, prompt_tokens, completion_tokens, cost_usd, context_tokens,",
        "               persona_tokens, summary_tokens, long_tokens, task_tokens, folded_messages,",
        f"               dropped_messages, estimated FROM usage WHERE agent_id = '{agent_id}' ORDER BY id;",
        "",
        f"{'обр.':>5} {'стратегия':<12} {'выз.':>5} {'запрос':>8} {'ответ':>7} {'стоимость':>10} "
        f"{'накоплено':>10} {'контекст':>9} {'профиль':>8} {'суммаризация':>12} {'долгоср.':>8} "
        f"{'задача':>6} {'вместо':>6} {'за окном':>8} {'оценка':>8} {'расх.':>7}",
        f"{'-' * 5} {'-' * 12} {'-' * 5} {'-' * 8} {'-' * 7} {'-' * 10} {'-' * 10} {'-' * 9} {'-' * 8} "
        f"{'-' * 12} {'-' * 8} {'-' * 6} {'-' * 6} {'-' * 8} {'-' * 8} {'-' * 7}",
    ]
    running = 0.0
    for row in rows:
        running += row["cost_usd"] or 0.0
        estimated, actual = row["estimated"], row["context_tokens"]
        error = f"{(estimated - actual) / actual * 100:+.1f}%" if estimated and actual else "—"
        # «+сум» в колонке стратегии — в этом обращении поверх неё работало сжатие.
        strategy = config.strategy_short(row.get("strategy") or "") or "—"
        if row.get("summarize"):
            strategy += "+сум"
        lines.append(
            f"{row['turn']:>5} {strategy:<12} "
            f"{row['llm_calls']:>5} {row['prompt_tokens']:>8} "
            f"{row['completion_tokens']:>7} {_money(row['cost_usd']):>10} {_money(running):>10} "
            f"{actual:>9} {row.get('persona_tokens') or 0:>8} {row.get('summary_tokens') or 0:>12} "
            f"{row.get('long_tokens') or row.get('facts_tokens') or 0:>8} "
            f"{row.get('task_tokens') or 0:>6} "
            f"{row.get('folded_messages') or 0:>6} {row.get('dropped_messages') or 0:>8} "
            f"{estimated:>8} {error:>7}"
        )
    if not rows:
        lines.append("-- строк нет: агент ещё не потратил ни одного токена")
    else:
        lines += [
            "",
            "-- «запрос» и «ответ» — факт по всем вызовам обращения (план, шаги, итог, суммаризация,",
            "-- маршрутизатор, теневой ответ), «контекст» — вес первого запроса, «профиль» — сколько",
            "-- в нём занял профиль пользователя (он платится в каждом запросе), «суммаризация»,"
            "-- «долгоср.» и «задача» — сколько в нём заняли блоки слоёв памяти, «вместо» — скольких",
            "-- сообщений вместо суммаризация, «за окном» — сколько сообщений истории не ушло дословно,",
            "-- «оценка» — что счётчик обещал до отправки. «+сум» у стратегии — в этом обращении поверх",
            "-- неё было включено сжатие истории.",
        ]

    view = QPlainTextEdit("\n".join(lines))
    view.setObjectName("raw")
    view.setReadOnly(True)
    view.setLineWrapMode(QPlainTextEdit.NoWrap)
    return view


def _accuracy_text(calibration: dict) -> str:
    """Насколько счётчику можно верить — словами."""
    if not calibration["samples"]:
        return ("Сверять пока не с чем: оценка считается по символам до отправки, а точное "
                "число приходит в `usage` вместе с ответом. Задайте агенту вопрос — и здесь "
                "появится расхождение.")
    return (
        f"Замеров: {calibration['samples']}, среднее расхождение оценки с фактом — "
        f"{calibration['error_pct']}%, накопленная поправка ×{calibration['factor']}. "
        f"Последняя сверка: счётчик обещал {_num(calibration['last_estimated'])} токенов, "
        f"модель насчитала {_num(calibration['last_actual'])}. Поправка живёт на каждую модель "
        f"отдельно: у них разные токенайзеры."
    )


def _forecast_text(state: dict, rows: list[dict]) -> str:
    """Прогноз: на сколько обменов хватит окна и что случится, когда оно кончится."""
    room = state["limit"] - state["reserve"] - state["context_tokens"]
    tail = "Когда места не останется, агент начнёт выбрасывать из окна самые старые пары " \
           "«вопрос-ответ»: в базе они сохранятся, но в модель уже не уйдут — начало разговора " \
           "агент забудет. Запрос, который не помещается даже без памяти, он отклонит сам, " \
           "не тратя вызов."
    if state["summarize"] or state["strategy"] == "facts":
        block = "суммаризацию" if state["summarize"] else "долговременную память"
        tail = f"Окно памяти при этом не растёт: старое уходит в {block}, а блок держится " \
               "примерно одного размера, поэтому до потолка дело обычно не доходит. " + tail
    dropped = sum(row["trimmed_pairs"] for row in rows)
    if dropped:
        tail += f" Пар, уже выброшенных из окна за всё время: {dropped}."

    growth = 0.0
    if len(rows) >= 2:
        growth = (rows[-1]["context_tokens"] - rows[0]["context_tokens"]) / (len(rows) - 1)
    if growth <= 0:
        return (f"Свободно ещё {_num(max(0, room))} токенов окна. Роста пока не видно — "
                f"нужно хотя бы пара обращений подряд, чтобы его измерить. " + tail)

    turns_left = int(room / growth)
    cost = [row["cost_usd"] or 0.0 for row in rows]
    average = sum(cost) / len(cost) if cost else 0.0
    return (
        f"Каждое обращение прибавляет к контексту в среднем {_num(round(growth))} токенов. "
        f"Свободно {_num(max(0, room))} — значит, окна хватит примерно на {_num(turns_left)} "
        f"обменов при нынешней длине реплик. Средняя цена обращения сейчас {_money(average)}, "
        f"то есть десяток следующих обменов обойдётся около {_money(average * 10)} "
        f"(теоретически: пока жива бесплатная квота, деньги не списываются). " + tail
    )


def _num(value: float | int) -> str:
    """Число с пробелами по тысячам: 1 000 000 читается, 1000000 — нет."""
    return f"{int(value):,}".replace(",", " ")


def _money(value: float | None) -> str:
    """Стоимость в долларах; None — цены у модели нет."""
    if value is None:
        return "—"
    return f"${value:.2f}" if value >= 1 else f"${value:.6f}"


def _strategy_hint(code: str, memory_turns: int) -> str:
    """Подсказка стратегии с настоящими числами вместо «N пар».

    Глубина памяти — отдельная настройка, и в подсказке стояло абстрактное N: по нему
    не видно, что это поле «ГЛУБИНА ПАМЯТИ, ПАР» прямо над переключателем. Подставляем
    её значение и то же самое в сообщениях — считать пары в уме незачем.
    """
    template = config.STRATEGY_BY_CODE.get(code, {}).get("hint", "")
    pairs = max(0, int(memory_turns))
    return template.format(pairs=_plural(pairs, "пара", "пары", "пар"), messages=pairs * 2)


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Число с существительным в нужном падеже: 1 пара, 3 пары, 10 пар."""
    tail, tens = count % 10, count % 100
    if tail == 1 and tens != 11:
        word = one
    elif 2 <= tail <= 4 and not 12 <= tens <= 14:
        word = few
    else:
        word = many
    return f"{count} {word}"


def _when(moment: float | None) -> str:
    """Когда это было, по-человечески: «сегодня в 21:14», «вчера в 9:05», «06.09 в 18:30»."""
    if not moment:
        return "ещё не говорили"
    day = time.localtime(moment)
    today = time.localtime()
    delta = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1)) - \
        time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1))
    days = round(delta / 86400)
    when = {0: "сегодня", 1: "вчера"}.get(days, time.strftime("%d.%m", day))
    return f"{when} в {time.strftime('%H:%M', day)}"


def _clip(text: str, limit: int) -> str:
    """Обрезать длинный результат инструмента: полный текст есть в сыром обмене."""
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n…[ещё {len(text) - limit} символов]"


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section")
    return label


def _ghost(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("ghost")
    button.setCursor(Qt.PointingHandCursor)
    return button


def _stat(caption: str) -> tuple[QLabel, QFrame]:
    """Карточка счётчика: крупное число и подпись под ним."""
    card = QFrame()
    card.setObjectName("stat")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(12, 8, 12, 9)
    lay.setSpacing(0)
    value = QLabel("0")
    value.setObjectName("statValue")
    label = QLabel(caption)
    label.setObjectName("statLabel")
    lay.addWidget(value)
    lay.addWidget(label)
    return value, card


def _dark_titlebar(window: QWidget) -> None:
    """Тёмная рамка окна на Windows — иначе светлый заголовок бьётся с темой."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            int(window.winId()), 20, ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int)
        )
    except Exception:
        pass


def build_app() -> tuple[QApplication, "AgentWindow"]:
    """Собрать приложение и окно (отдельно от main, чтобы окно можно было проверять)."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")  # предсказуемая база под QSS, одинаковая на всех системах
    palette = app.palette()
    palette.setColor(QPalette.Window, QColor(BLACK))
    palette.setColor(QPalette.Base, QColor(CARD))
    palette.setColor(QPalette.Text, QColor(TEXT))
    palette.setColor(QPalette.WindowText, QColor(TEXT))
    palette.setColor(QPalette.Highlight, QColor(ACCENT2))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(QSS)
    return app, AgentWindow()


def main() -> None:
    app, window = build_app()
    window.show()
    window.apply_saved_scale()
    _dark_titlebar(window)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
