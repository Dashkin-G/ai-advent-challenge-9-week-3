"""Инструменты агента: то, чем он умеет действовать, а не только говорить.

Каждый инструмент — обычная функция плюс описание и JSON-схема аргументов. Схемы
уходят в модель вместе с запросом, модель сама решает, что вызвать, а исполняет
всё агент (см. цикл в app/agent.py). Здесь нет ни одного обращения к LLM: этот
модуль ничего не знает про модель, он только делает работу.

Границы намеренно узкие — агент действует, и цена ошибки выше, чем в чате:
- файловые инструменты живут только внутри песочницы `workspace/`, путь
  нормализуется и проверяется, выход за пределы запрещён;
- выполнения кода и команд оболочки нет вообще;
- сетевой инструмент ходит только по http/https, с таймаутом и лимитом размера;
- результат любого инструмента усечён, чтобы не раздувать контекст и счёт.

Ошибку инструмента возвращаем модели текстом (а не роняем обращение) — так агент
может её увидеть и попробовать другой путь.
"""
import ast
import json
import operator
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from . import config


class ToolError(Exception):
    """Инструмент не смог выполнить работу: агент покажет это модели как результат."""


@dataclass(frozen=True)
class Tool:
    """Инструмент: имя, описание для модели, схема аргументов и сама функция."""
    name: str
    description: str
    parameters: dict          # JSON Schema аргументов
    run: Callable[..., Any]
    title: str                # короткое человекочитаемое имя для интерфейса


# --- песочница ---------------------------------------------------------------

def workspace() -> Path:
    """Рабочая папка агента, создаётся при первом обращении."""
    config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    return config.WORKSPACE_DIR


_ABSOLUTE_RE = re.compile(r"^(/|\\|[A-Za-z]:)")


def _safe_path(path: str) -> Path:
    """Привести путь к абсолютному внутри песочницы или отказать.

    Абсолютные пути отклоняем явно, а не подставляем молча в песочницу: иначе
    «/etc/passwd» тихо превратился бы в «workspace/etc/passwd» и модель считала бы,
    что прочитала системный файл. После склейки ещё раз проверяем, что результат
    не вышел за пределы рабочей папки (это ловит `..` в любом виде).
    """
    raw = str(path or "").strip().replace("\\", "/")
    if _ABSOLUTE_RE.match(raw):
        raise ToolError("Путь должен быть относительным: файловые инструменты работают "
                        "только внутри рабочей папки агента.")
    root = workspace().resolve()
    candidate = (root / raw).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError("Путь ведёт за пределы рабочей папки агента — отказано.")
    return candidate


def _rel(path: Path) -> str:
    return path.relative_to(workspace().resolve()).as_posix() or "."


def _clip(text: str, limit: int | None = None) -> str:
    limit = limit or config.TOOL_RESULT_CHARS
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[обрезано, всего {len(text)} символов]"


# --- реализации инструментов -------------------------------------------------

def tool_now() -> str:
    """Текущие дата и время — того, чего модель принципиально не знает."""
    now = datetime.now().astimezone()
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    return f"{now:%Y-%m-%d %H:%M:%S %Z}, {days[now.weekday()]}"


_CALC_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _calc_node(node: ast.AST) -> float:
    """Обход дерева выражения: разрешены только числа и арифметика, без имён и вызовов."""
    if isinstance(node, ast.Expression):
        return _calc_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_OPS:
        return _CALC_OPS[type(node.op)](_calc_node(node.left), _calc_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_OPS:
        return _CALC_OPS[type(node.op)](_calc_node(node.operand))
    raise ToolError("В выражении разрешены только числа и арифметика: + - * / // % **")


def tool_calc(expression: str) -> str:
    """Посчитать арифметику точно. Разбор через ast — никакого eval."""
    expression = (expression or "").strip()
    if not expression:
        raise ToolError("Пустое выражение.")
    if len(expression) > 200:
        raise ToolError("Выражение слишком длинное.")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise ToolError(f"Не разобрать выражение: {e.msg}") from e
    try:
        value = _calc_node(tree)
    except ZeroDivisionError as e:
        raise ToolError("Деление на ноль.") from e
    return f"{expression} = {value}"


def tool_list_dir(path: str = "") -> str:
    """Что лежит в песочнице: файлы с размерами, папки отдельно."""
    target = _safe_path(path)
    if not target.exists():
        raise ToolError(f"Папки «{path}» нет в рабочей папке агента.")
    if not target.is_dir():
        raise ToolError(f"«{path}» — это файл, а не папка.")
    items = []
    for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        items.append(f"{_rel(entry)}/" if entry.is_dir() else f"{_rel(entry)} ({entry.stat().st_size} байт)")
    return "\n".join(items) if items else "Рабочая папка пуста."


def tool_read_file(path: str) -> str:
    """Прочитать текстовый файл из песочницы."""
    target = _safe_path(path)
    if not target.is_file():
        raise ToolError(f"Файла «{path}» нет в рабочей папке агента.")
    try:
        return _clip(target.read_text(encoding="utf-8"))
    except UnicodeDecodeError as e:
        raise ToolError("Файл не текстовый (не читается как UTF-8).") from e


def tool_write_file(path: str, content: str) -> str:
    """Записать текстовый файл в песочницу (папки создаются автоматически)."""
    target = _safe_path(path)
    if target.is_dir():
        raise ToolError(f"«{path}» — это папка.")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content or ""
    target.write_text(data, encoding="utf-8")
    return f"Записано в {_rel(target)}: {len(data.encode('utf-8'))} байт."


def tool_search_files(query: str) -> str:
    """Найти строку по всем текстовым файлам песочницы (как grep)."""
    query = (query or "").strip()
    if not query:
        raise ToolError("Пустой запрос поиска.")
    hits = []
    for file in sorted(workspace().rglob("*")):
        if not file.is_file():
            continue
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(lines, 1):
            if query.lower() in line.lower():
                hits.append(f"{_rel(file)}:{number}: {line.strip()[:160]}")
                if len(hits) >= 50:
                    return "\n".join(hits) + "\n…[показаны первые 50 совпадений]"
    return "\n".join(hits) if hits else f"Совпадений с «{query}» не найдено."


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


def tool_http_get(url: str) -> str:
    """Скачать страницу и вернуть её текст без разметки."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ToolError("Разрешены только адреса http:// и https://")
    try:
        response = httpx.get(url, timeout=config.HTTP_TIMEOUT, follow_redirects=True,
                             headers={"User-Agent": "ai-advent-agent/1.0"})
        response.raise_for_status()
    except httpx.HTTPError as e:
        raise ToolError(f"Не удалось скачать страницу: {e}") from e
    body = response.text
    if "html" in response.headers.get("content-type", ""):
        body = _TAG_RE.sub(" ", body)
        body = re.sub(r"\s+", " ", body)
    return _clip(body.strip(), 2000)


def tool_weather(city: str) -> str:
    """Погода сейчас по названию города (open-meteo, без ключа и регистрации)."""
    city = (city or "").strip()
    if not city:
        raise ToolError("Не указан город.")
    try:
        found = httpx.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "ru", "format": "json"},
            timeout=config.HTTP_TIMEOUT,
        ).json().get("results")
        if not found:
            raise ToolError(f"Город «{city}» не найден.")
        place = found[0]
        current = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"], "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,wind_speed_10m,relative_humidity_2m",
            },
            timeout=config.HTTP_TIMEOUT,
        ).json()["current"]
    except httpx.HTTPError as e:
        raise ToolError(f"Сервис погоды недоступен: {e}") from e
    return (
        f"{place['name']} ({place.get('country', '')}): "
        f"{current['temperature_2m']}°C, ощущается {current['apparent_temperature']}°C, "
        f"влажность {current['relative_humidity_2m']}%, ветер {current['wind_speed_10m']} км/ч "
        f"(на {current['time']})"
    )


# --- реестр ------------------------------------------------------------------

def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


TOOLS: list[Tool] = [
    Tool(
        name="now", title="текущее время",
        description="Текущие дата, время и день недели. Вызывай, когда нужна актуальная дата.",
        parameters=_schema({}, []),
        run=tool_now,
    ),
    Tool(
        name="calc", title="калькулятор",
        description="Точно посчитать арифметическое выражение (+ - * / // % **). Используй вместо счёта в уме.",
        parameters=_schema({"expression": {"type": "string", "description": "Например: (120*3+18)/7"}}, ["expression"]),
        run=tool_calc,
    ),
    Tool(
        name="list_dir", title="список файлов",
        description="Список файлов и папок в рабочей папке агента.",
        parameters=_schema({"path": {"type": "string", "description": "Подпапка; пусто — корень рабочей папки"}}, []),
        run=tool_list_dir,
    ),
    Tool(
        name="read_file", title="чтение файла",
        description="Прочитать текстовый файл из рабочей папки агента.",
        parameters=_schema({"path": {"type": "string", "description": "Путь относительно рабочей папки"}}, ["path"]),
        run=tool_read_file,
    ),
    Tool(
        name="write_file", title="запись файла",
        description="Создать или перезаписать текстовый файл в рабочей папке агента.",
        parameters=_schema({
            "path": {"type": "string", "description": "Путь относительно рабочей папки, например report.md"},
            "content": {"type": "string", "description": "Полное содержимое файла"},
        }, ["path", "content"]),
        run=tool_write_file,
    ),
    Tool(
        name="search_files", title="поиск по файлам",
        description="Найти строку во всех текстовых файлах рабочей папки; возвращает файл, строку и её номер.",
        parameters=_schema({"query": {"type": "string", "description": "Что искать"}}, ["query"]),
        run=tool_search_files,
    ),
    Tool(
        name="http_get", title="загрузка страницы",
        description="Скачать страницу по адресу http/https и вернуть её текст без разметки.",
        parameters=_schema({"url": {"type": "string", "description": "Полный адрес страницы"}}, ["url"]),
        run=tool_http_get,
    ),
    Tool(
        name="weather", title="погода",
        description="Погода сейчас в указанном городе (температура, ветер, влажность).",
        parameters=_schema({"city": {"type": "string", "description": "Название города"}}, ["city"]),
        run=tool_weather,
    ),
]

BY_NAME = {t.name: t for t in TOOLS}


def specs() -> list[dict]:
    """Описания инструментов в формате OpenAI tools — уходят в модель."""
    return [
        {"type": "function",
         "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in TOOLS
    ]


def catalog() -> list[dict]:
    """Список инструментов для интерфейса: что агент вообще умеет делать."""
    return [{"name": t.name, "title": t.title, "description": t.description} for t in TOOLS]


def call(name: str, arguments: dict | str) -> str:
    """Выполнить инструмент по имени. Ошибки поднимаются как ToolError."""
    tool = BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"Инструмента «{name}» нет. Доступны: {', '.join(BY_NAME)}.")

    if isinstance(arguments, str):  # модель присылает аргументы строкой JSON
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError as e:
            raise ToolError(f"Аргументы не разобрать как JSON: {e}") from e
    if not isinstance(arguments, dict):
        raise ToolError("Аргументы должны быть объектом JSON.")

    allowed = set(tool.parameters.get("properties", {}))
    unknown = set(arguments) - allowed
    if unknown:
        raise ToolError(f"Лишние аргументы: {', '.join(sorted(unknown))}. Разрешены: {', '.join(sorted(allowed))}.")
    missing = set(tool.parameters.get("required", [])) - set(arguments)
    if missing:
        raise ToolError(f"Не хватает аргументов: {', '.join(sorted(missing))}.")

    try:
        return _clip(str(tool.run(**arguments)))
    except ToolError:
        raise
    except Exception as e:  # чтобы сбой инструмента не ронял всё обращение
        raise ToolError(f"{type(e).__name__}: {e}") from e
