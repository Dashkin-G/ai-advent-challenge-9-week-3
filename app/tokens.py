"""Счётчик токенов: во что обойдётся запрос ещё до того, как он ушёл в модель.

Точное число токенов знает только токенайзер модели, а он живёт на стороне
провайдера: в ответе приходит `usage`, но это уже постфактум — деньги потрачены,
контекст переполнен, разговор сломался. Агенту число нужно РАНЬШЕ вызова, поэтому
здесь своя оценка: посимвольная, без новых зависимостей и без похода в сеть.

Идея простая. BPE режет текст на куски примерно постоянной длины, и длина куска
зависит от алфавита: латиница — около 3.5 символов на токен, кириллица — около
3.3, иероглиф — почти всегда токен целиком, а цифра — ровно токен, потому что
числа режутся поразрядно. Считаем символы по классам, делим на эти коэффициенты,
добавляем служебную разметку диалога — получается оценка с ошибкой около 7%.

Дальше оценка подтягивается к реальности: в каждом ответе модель присылает точный
`usage`, и `observe()` подправляет множитель для этой модели. Через несколько
обращений оценка сходится к настоящему токенайзеру, а в интерфейсе видно, на
сколько процентов она разошлась с фактом.

Модуль ничего не знает ни про агента, ни про API, ни про интерфейс: на входе —
текст и сообщения, на выходе — числа. Границы те же, что у `llm.py` и `store.py`.
"""
import json
import math
import re
from dataclasses import dataclass

# Сколько символов приходится на один токен. Коэффициенты подобраны по живым
# замерам: дюжина разных запросов (русский текст, английский, JSON, markdown,
# числа, схемы инструментов) ушла в модель, а фактический `prompt_tokens` из
# `usage` сравнивался с оценкой. Средняя ошибка получившегося набора — около 7%,
# худший случай — 15%; дальше её добирает калибровка.
CHARS_PER_TOKEN = {
    "cjk": 1.0,        # иероглиф почти всегда отдельный токен
    "cyrillic": 3.3,   # русский текст: примерно треть токена на букву
    "latin": 3.5,      # классическая оценка для английского
    "digit": 1.0,      # числа режутся по одной цифре — это дорого и неочевидно
    "other": 1.7,      # пунктуация и скобки: в JSON их много, и они платные
}

# Служебная разметка: у каждого сообщения свои границы роли (<|im_start|>role …
# <|im_end|>), у запроса — затравка ответа и обвязка провайдера, а перенос строки
# и отступ становятся отдельными токенами (поэтому JSON с indent дороже плоского).
MESSAGE_OVERHEAD = 4
REQUEST_OVERHEAD = 8
NEWLINE_TOKENS = 0.8
INDENT_TOKENS = 1.7      # группа из двух и более пробелов подряд
TOOL_CALL_OVERHEAD = 6   # обёртка одного запроса инструмента в сообщении ассистента

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LATIN = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"[0-9]")
_INLINE_SPACE = re.compile(r"[ \t\r]")
_NEWLINE = re.compile(r"\n")
_INDENT = re.compile(r"[ \t]{2,}")


def count(text: str) -> int:
    """Оценка числа токенов в куске текста (без служебной разметки диалога)."""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    cyrillic = len(_CYRILLIC.findall(text))
    latin = len(_LATIN.findall(text))
    digit = len(_DIGIT.findall(text))
    newline = len(_NEWLINE.findall(text))
    indent = len(_INDENT.findall(text))
    # Одиночный пробел BPE приклеивает к следующему слову, поэтому сам по себе он
    # ничего не стоит. А вот перенос строки и отступ из нескольких пробелов подряд
    # становятся отдельными токенами — из-за них форматированный JSON вдвое дороже
    # такого же, но записанного в одну строку.
    other = len(text) - cjk - cyrillic - latin - digit - newline - len(_INLINE_SPACE.findall(text))
    estimate = (
        cjk / CHARS_PER_TOKEN["cjk"]
        + cyrillic / CHARS_PER_TOKEN["cyrillic"]
        + latin / CHARS_PER_TOKEN["latin"]
        + digit / CHARS_PER_TOKEN["digit"]
        + max(0, other) / CHARS_PER_TOKEN["other"]
        + newline * NEWLINE_TOKENS
        + indent * INDENT_TOKENS
    )
    return max(1, math.ceil(estimate))


def count_message(message: dict) -> int:
    """Оценка одного сообщения диалога вместе с его служебной разметкой."""
    total = MESSAGE_OVERHEAD + count(str(message.get("role") or ""))
    total += count(message.get("content") or "")
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += TOOL_CALL_OVERHEAD
        total += count(function.get("name") or "") + count(function.get("arguments") or "")
    if message.get("tool_call_id"):
        total += MESSAGE_OVERHEAD  # ссылка на вызов тоже уезжает в запрос
    return total


def count_messages(messages: list[dict]) -> int:
    """Оценка списка сообщений — ровно то, что уходит в модель одним запросом."""
    if not messages:
        return 0
    return sum(count_message(m) for m in messages) + REQUEST_OVERHEAD


def count_tools(specs: list[dict] | None) -> int:
    """Оценка описаний инструментов: схемы уезжают в тот же запрос и тоже платные.

    Про них забывают чаще всего: восемь инструментов с JSON-схемами стоят столько
    же, сколько несколько пар «вопрос-ответ» в памяти.
    """
    if not specs:
        return 0
    return count(json.dumps(specs, ensure_ascii=False))


@dataclass
class Calibration:
    """Поправка оценки под токенайзер конкретной модели.

    Оценка по символам систематически ошибается в одну сторону — на сколько
    именно, показывает первый же ответ с `usage`. Множитель двигаем плавно
    (весом), чтобы одно нетипичное обращение не увело счётчик.
    """
    model: str = ""
    factor: float = 1.0
    samples: int = 0
    last_estimated: int = 0
    last_actual: int = 0
    error_sum: float = 0.0    # сумма модулей относительных ошибок — для средней

    WEIGHT = 0.4              # доля новой поправки: 1.0 — верить последнему замеру
    MIN_FACTOR = 0.4
    MAX_FACTOR = 2.5

    def apply(self, raw: int) -> int:
        """Пересчитать сырую оценку с поправкой этой модели."""
        return max(1, round(raw * self.factor)) if raw else 0

    def observe(self, estimated: int, actual: int) -> None:
        """Сверить показанную оценку с фактом из `usage` и подправить множитель."""
        if estimated <= 0 or actual <= 0:
            return
        self.samples += 1
        self.last_estimated, self.last_actual = estimated, actual
        self.error_sum += abs(actual - estimated) / actual
        ratio = actual / estimated
        factor = self.factor * (ratio ** self.WEIGHT)
        self.factor = min(self.MAX_FACTOR, max(self.MIN_FACTOR, factor))

    @property
    def error_pct(self) -> float | None:
        """Средняя ошибка оценки в процентах (None, пока не с чем сравнивать)."""
        return round(self.error_sum / self.samples * 100, 1) if self.samples else None

    @property
    def last_error_pct(self) -> float | None:
        """Ошибка последнего замера в процентах: со знаком, чтобы видеть перекос."""
        if not self.last_actual:
            return None
        return round((self.last_estimated - self.last_actual) / self.last_actual * 100, 1)


_CALIBRATIONS: dict[str, Calibration] = {}


def calibration(model: str | None) -> Calibration:
    """Поправка для модели: у каждой свой токенайзер, значит и свой множитель."""
    key = model or ""
    if key not in _CALIBRATIONS:
        _CALIBRATIONS[key] = Calibration(model=key)
    return _CALIBRATIONS[key]


def observe(model: str | None, estimated: int, actual: int) -> None:
    """Запомнить расхождение оценки с фактическим `usage` этой модели."""
    calibration(model).observe(estimated, actual)


@dataclass(frozen=True)
class Breakdown:
    """Из чего сложился запрос: во что обходится каждая его часть.

    Ровно это и объясняет поведение агента: инструкция и схемы инструментов
    платятся в каждом обращении, память растёт с диалогом, а сам вопрос
    пользователя — обычно самая маленькая часть счёта. Части `summary`, `long` и
    `task` — это слои памяти: они лежат внутри system-сообщения, но считаются
    отдельно, потому что их вес и есть цена каждого слоя.
    """
    system: int = 0
    summary: int = 0
    long: int = 0      # долговременная память: профиль, решения, знания
    task: int = 0      # рабочая память: карточка текущей задачи
    memory: int = 0
    question: int = 0
    tools: int = 0

    @property
    def total(self) -> int:
        return (self.system + self.summary + self.long + self.task
                + self.memory + self.question + self.tools)

    def parts(self) -> list[tuple[str, int]]:
        """Части в порядке показа — интерфейсу удобно рисовать их полосой."""
        return [
            ("инструкция", self.system),
            ("суммаризация", self.summary),
            ("долговременная", self.long),
            ("задача", self.task),
            ("память", self.memory),
            ("вопрос", self.question),
            ("схемы инструментов", self.tools),
        ]

    def to_dict(self) -> dict:
        return {
            "system": self.system,
            "summary": self.summary,
            "long": self.long,
            "task": self.task,
            "memory": self.memory,
            "question": self.question,
            "tools": self.tools,
            "total": self.total,
        }


def measure(
    system: str,
    memory: list[dict],
    question: str = "",
    tool_specs: list[dict] | None = None,
    model: str | None = None,
    summary: str = "",
    long: str = "",
    task: str = "",
) -> Breakdown:
    """Оценить будущий запрос по частям, с поправкой на токенайзер модели.

    `summary`, `long` и `task` — блоки слоёв памяти, уже вписанные в `system`: их
    вес вычитается из инструкции и показывается отдельными частями. Сумма частей
    при этом остаётся весом целого запроса.
    """
    fix = calibration(model)
    whole = count_message({"role": "system", "content": system}) + REQUEST_OVERHEAD
    folded = min(whole, count(summary)) if summary else 0
    sticky = min(whole - folded, count(long)) if long else 0
    working = min(whole - folded - sticky, count(task)) if task else 0
    return Breakdown(
        system=fix.apply(whole - folded - sticky - working),
        summary=fix.apply(folded),
        long=fix.apply(sticky),
        task=fix.apply(working),
        memory=fix.apply(sum(count_message(m) for m in memory)),
        question=fix.apply(count_message({"role": "user", "content": question})) if question else 0,
        tools=fix.apply(count_tools(tool_specs)),
    )


def measure_messages(messages: list[dict], model: str | None = None) -> int:
    """Оценить набор сообщений без обвязки запроса — например, окно памяти."""
    return calibration(model).apply(sum(count_message(m) for m in messages))


def measure_text(text: str, model: str | None = None) -> int:
    """Оценить кусок текста с поправкой модели — например, сама суммаризация."""
    return calibration(model).apply(count(text)) if text else 0


def measure_request(
    messages: list[dict],
    tool_specs: list[dict] | None = None,
    model: str | None = None,
) -> int:
    """Оценить готовый запрос целиком: сообщения плюс схемы инструментов."""
    return calibration(model).apply(count_messages(messages) + count_tools(tool_specs))


def measure_history(messages: list[dict], model: str | None = None) -> int:
    """Оценить всю переписку целиком — включая то, что в контекст уже не попадает."""
    return calibration(model).apply(count_messages(messages))
