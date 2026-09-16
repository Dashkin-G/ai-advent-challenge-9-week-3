"""Персонализация: профиль пользователя поверх модели памяти.

Слои памяти (app/memory.py) отвечают на вопрос «что агент помнит». Этот модуль —
на другой: «для кого он говорит». Профиль пользователя описывает собеседника и
требования к форме ответа — стиль, формат, ограничения — и подключается к каждому
запросу отдельным блоком инструкции, рядом с блоками слоёв.

Почему это отдельная сущность, а не ещё один вид записи долговременной памяти:

    запись памяти   факт о разговоре: «имя: Сергей». Появляется сама, живёт в
                    ветке, переживает задачи — но остаётся фактом;
    профиль         требование к ответу: «объясняй как новичку, без кода, до 70
                    слов». Его выбирают, между профилями переключаются, и один и
                    тот же вопрос при разных профилях получает разные ответы.

Профиль наполняется тремя способами — как и слои памяти, и каждое значение помнит,
кто его вписал (`source`): человек в окне правки (`user`), маршрутизатор после
ответа (`router`) и сам агент инструментом `prefer` (`tool`). «Кто собеседник»
(`about`) правит только человек: то, что агент узнаёт о нём по ходу разговора, —
это долговременная память, и дублировать её здесь незачем.

Главное отличие от просьбы в промпте: часть профиля ПРОВЕРЯЕТСЯ кодом после
генерации (`check`). Длина считается в словах, формат — по разметке ответа,
ограничения из реестра — по своим признакам. Без этого «агент учитывает профиль»
осталось бы обещанием, которое некому опровергнуть (тот же довод, что у
инвариантов дня 11). Свои ограничения, вписанные словами, так и помечены —
просьба, а не проверка.

Границы те же, что у `memory.py`: модуль не знает ни про API модели, ни про базу,
ни про Qt. Он описывает профиль и правила, а зовёт их агент; `store.py` хранит,
`gui.py` показывает.
"""
import re
import time
from dataclasses import dataclass, field

from . import config

# --- Блок, которым профиль уходит в инструкцию -------------------------------
# Модели мало отдать анкету: без объяснения она читает её как факты о собеседнике
# и продолжает отвечать в своей обычной манере. Поэтому в заметке прямо сказано,
# что это требования к самому ответу и что их проверяют.
PERSONA_NOTE = (
    "\n\nНиже — профиль твоего собеседника: кто он и в какой форме просит отвечать. Это не "
    "справка о человеке, а требования к твоему ответу, и они действуют в каждом ответе, даже "
    "если вопрос короткий. Агент сверяет готовый ответ с профилем и показывает пользователю "
    "расхождения, так что соблюдай их буквально: и стиль, и формат, и длину, и ограничения. "
    "Если требование профиля мешает ответить по существу — ответь по существу и одной строкой "
    "скажи, в чём пришлось отступить.\nПрофиль пользователя «{name}»:\n{profile}"
)

# Что маршрутизатор может менять в профиле. Правило отдельное от правил памяти:
# профиль — не слой, и путать их нельзя, иначе в память поедут требования к форме,
# а в профиль — факты о человеке.
ROUTER_RULES = (
    "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ — то, КАК он просит отвечать (форма, а не факты). Меняй его, только "
    "когда пользователь прямо сказал о форме ответа: «отвечай короче», «давай списком», «без "
    "кода», «объясняй проще», «не задавай вопросов». Верни ТОЛЬКО те поля, которые надо "
    "изменить, остальные не возвращай; ничего не менять — пустой объект.\n"
    "  style — стиль общения: {styles};\n"
    "  format — формат ответа: {formats};\n"
    "  length — длина ответа: {lengths};\n"
    "  limits — ограничения, список; из готовых: {limits}; своё — короткой фразой;\n"
    "  prefs — прочие предпочтения парами «ключ: значение» (например, «примеры»: «из съёмок»).\n"
    "Факты о человеке (имя, занятие, планы) в профиль НЕ клади — им место в долговременной "
    "памяти, вид profile.\n"
)

# Кусок JSON-схемы ответа маршрутизатора. Живёт здесь, а не в memory.py, чтобы
# модуль памяти ничего не знал про персонализацию: агент склеивает их сам.
ROUTER_SCHEMA = (
    '"persona": {"style": "...", "format": "...", "length": "...", "limits": ["..."], '
    '"prefs": {"ключ": "значение"}}'
)

ROUTER_INPUT = "Текущий профиль пользователя:\n{profile}"

# Строки, которыми агент объясняет отступление от профиля. Они не должны
# засчитываться нарушением: «слово „фича“ — англицизм, пишу „возможность“» — это
# соблюдение ограничения, а не его нарушение (тот же случай, что с отказом при
# проверке инвариантов дня 11).
_EXPLAIN_MARKERS = (
    "профил", "англицизм", "жаргон", "вместо", "не могу", "ограничени", "по-русски",
)

_LIST_LINE = re.compile(r"^\s*(?:[-—–*•·]|\d+[.)])\s+", re.MULTILINE)
_NUMBERED_LINE = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_WORD = re.compile(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*", re.UNICODE)
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿]"
)


# --- Профиль -----------------------------------------------------------------

@dataclass
class Pref:
    """Свободное предпочтение «ключ: значение» — то, чего нет в реестрах.

    Реестры покрывают три группы из задания, но предпочтения человека ими не
    исчерпываются: «примеры — из съёмок», «единицы — в рублях». Такие пары живут
    рядом с выбором из списков, а не внутри него.
    """
    key: str
    value: str
    source: str = "user"      # user / router / tool
    turn: int = 0
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value, "source": self.source,
                "turn": self.turn, "at": self.at}


@dataclass
class Persona:
    """Профиль пользователя: кто он и в какой форме хочет получать ответы.

    `style`, `format` и `length` — коды из реестров `config`, поэтому значение
    всегда можно и объяснить модели, и проверить в готовом ответе. `limits` —
    смесь кодов реестра и своих фраз: первые проверяются, вторые уходят просьбой.
    """
    id: str
    name: str = "Профиль"
    about: str = ""
    style: str = config.PERSONA_STYLE
    format: str = config.PERSONA_FORMAT
    length: str = config.PERSONA_LENGTH
    limits: list[str] = field(default_factory=list)
    prefs: list[Pref] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ---------------------------------------------------------------- показ --

    def text(self) -> str:
        """Профиль в том виде, в каком он уходит в инструкцию.

        Метки в квадратных скобках — та же разметка, что у карточки задачи: модель
        различает разделы, а человек видит в «сыром обмене», из чего блок собран.
        """
        lines = []
        if self.about:
            lines.append(f"[КТО] {self.about}")
        lines.append(f"[СТИЛЬ] {config.style_label(self.style)} — {self.style_rule()}")
        lines.append(f"[ФОРМАТ] {config.format_label(self.format)} — {self.format_rule()}")
        lines.append(f"[ДЛИНА] {config.length_label(self.length)} — {self.length_rule()}")
        if self.limits:
            lines.append("[ОГРАНИЧЕНИЯ]")
            lines.extend(f"— {self.limit_rule(code)}" for code in self.limits)
        if self.prefs:
            lines.append("[ПРЕДПОЧТЕНИЯ]")
            lines.extend(f"— {pref.key}: {pref.value}" for pref in self.prefs)
        return "\n".join(lines)

    def block(self) -> str:
        """Готовый блок инструкции — его агент вставляет в system-сообщение."""
        return PERSONA_NOTE.format(name=self.name, profile=self.text())

    def style_rule(self) -> str:
        item = config.STYLE_BY_CODE.get(self.style)
        return item["rule"] if item else ""

    def format_rule(self) -> str:
        item = config.FORMAT_BY_CODE.get(self.format)
        return item["rule"] if item else ""

    def length_rule(self) -> str:
        item = config.LENGTH_BY_CODE.get(self.length)
        return item["rule"] if item else ""

    @staticmethod
    def limit_rule(code: str) -> str:
        """Ограничение в том виде, в каком оно уходит модели: готовое — своим текстом."""
        item = config.LIMIT_BY_CODE.get(code)
        return item["rule"] if item else code

    def parts(self) -> list[str]:
        """Состав профиля по пунктам: «поле: значение», по пункту на строку.

        Каждое значение подписано тем, чем оно является. Без подписей строка
        «наставник · пошагово · коротко · 2 огранич.» читается как набор случайных
        слов: три из них — значения разных полей, четвёртое — счётчик, и понять,
        что к чему, можно только зная реестры наизусть.
        """
        items = [f"стиль: {config.style_label(self.style).lower()}",
                 f"формат: {config.format_label(self.format).lower()}",
                 f"длина: {config.length_label(self.length).lower()}"]
        if self.limits:
            items.append(f"ограничений: {len(self.limits)}")
        return items

    def summary(self) -> str:
        """Тот же состав одной строкой — для меню, мета-строк и широких мест.

        В узкую панель эта строка не помещается и переносится где попало, разрывая
        пары «поле: значение» пополам, — там показываются `parts()` по строке.
        """
        return " · ".join(self.parts())

    def size(self) -> int:
        """Сколько в профиле заполненных пунктов — мера «во что он обошёлся»."""
        return 3 + len(self.limits) + len(self.prefs) + (1 if self.about else 0)

    def checked_limits(self) -> list[str]:
        """Ограничения, которые код умеет проверить (остальные — просьба в промпте)."""
        return [code for code in self.limits if code in config.LIMIT_BY_CODE]

    # -------------------------------------------------------------- хранение --

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "about": self.about,
            "style": self.style,
            "style_label": config.style_label(self.style),
            "format": self.format,
            "format_label": config.format_label(self.format),
            "length": self.length,
            "length_label": config.length_label(self.length),
            "length_words": config.length_words(self.length),
            "limits": list(self.limits),
            "limit_labels": [config.limit_label(code) for code in self.limits],
            "checked": self.checked_limits(),
            "prefs": [pref.to_dict() for pref in self.prefs],
            "summary": self.summary(),
            "parts": self.parts(),
            "size": self.size(),
            "text": self.text(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def row(self) -> dict:
        """Строка таблицы `personas` — списки уезжают на диск как есть."""
        return {
            "id": self.id,
            "name": self.name,
            "about": self.about,
            "style": self.style,
            "format": self.format,
            "length": self.length,
            "limits": list(self.limits),
            "prefs": [pref.to_dict() for pref in self.prefs],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: dict) -> "Persona":
        """Строка базы → профиль. К значениям относимся как к чужим.

        Незнакомый код стиля, формата или длины заменяется значением по умолчанию:
        база могла пережить правку руками или прошлую версию реестра, и падать
        из-за этого приложение не должно.
        """
        return cls(
            id=str(row.get("id") or ""),
            name=str(row.get("name") or "Профиль"),
            about=str(row.get("about") or ""),
            style=_known(row.get("style"), config.STYLE_BY_CODE, config.PERSONA_STYLE),
            format=_known(row.get("format"), config.FORMAT_BY_CODE, config.PERSONA_FORMAT),
            length=_known(row.get("length"), config.LENGTH_BY_CODE, config.PERSONA_LENGTH),
            limits=[str(item)[:120] for item in (row.get("limits") or [])][:config.PERSONA_LIMITS_MAX],
            prefs=[
                Pref(key=str(item.get("key") or ""), value=str(item.get("value") or ""),
                     source=str(item.get("source") or "user"), turn=int(item.get("turn") or 0),
                     at=float(item.get("at") or 0) or time.time())
                for item in (row.get("prefs") or []) if item.get("key")
            ][:config.PERSONA_PREFS_LIMIT],
            created_at=float(row.get("created_at") or time.time()),
            updated_at=float(row.get("updated_at") or time.time()),
        )


# --- Что изменилось в профиле и соблюдён ли он -------------------------------

@dataclass
class Change:
    """Одна правка профиля: что изменилось и кто это решил."""
    field: str                # style / format / length / limits / prefs / about / name
    what: str                 # человекочитаемо: «формат: связный текст → списком»
    source: str               # user / router / tool
    action: str = "change"    # change / add / reject
    key: str = ""             # ключ предпочтения — по нему видно, что уже занято

    def to_dict(self) -> dict:
        return {"field": self.field, "what": self.what, "source": self.source,
                "action": self.action, "key": self.key}


def locked_by(changes: list[Change]) -> frozenset:
    """Что на этом обращении уже правил агент инструментом — и трогать это нельзя.

    Маршрутизатор работает после ответа и не видит, что агент успел положить в
    профиль сам: он перезаписывает ту же строку своими словами, и в трассе вместо
    одной правки появляются две, причём с чужим источником. Та же защита, что у
    записей долговременной памяти в `memory.merge_long`.
    """
    locked = set()
    for change in changes:
        if change.source != "tool":
            continue
        locked.add(f"pref:{change.key.lower()}" if change.field == "prefs" else change.field)
    return frozenset(locked)


@dataclass
class Check:
    """Одна проверка готового ответа на соответствие профилю."""
    label: str      # что проверяли: «длина ≤ 70 слов»
    ok: bool
    detail: str     # чем кончилось: «в ответе 54 слова»

    def to_dict(self) -> dict:
        return {"label": self.label, "ok": self.ok, "detail": self.detail}


@dataclass
class PersonaUpdate:
    """Что случилось с профилем на этом обращении: правки и результат проверки."""
    name: str = ""
    changes: list[Change] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)   # что маршрутизатор попросил зря
    checks: list[Check] = field(default_factory=list)
    tokens: int = 0                  # во что профиль обошёлся в этом запросе

    def __bool__(self) -> bool:
        return bool(self.changes or self.rejected or self.checks)

    @property
    def broken(self) -> list[Check]:
        """Нарушенные требования профиля — то, что видно под ответом."""
        return [check for check in self.checks if not check.ok]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "changes": [change.to_dict() for change in self.changes],
            "rejected": list(self.rejected),
            "checks": [check.to_dict() for check in self.checks],
            "broken": [check.to_dict() for check in self.broken],
            "tokens": self.tokens,
        }


# --- Маршрутизация: что профиль берёт из ответа маршрутизатора ---------------

def router_rules() -> str:
    """Правила профиля для роли маршрутизатора — со списком допустимых значений.

    Значения подставляются из реестров, а не пишутся текстом: добавили стиль в
    `config` — модель узнала о нём в тот же момент, и второго места для правки нет.
    """
    return ROUTER_RULES.format(
        styles=_listing(config.PERSONA_STYLES),
        formats=_listing(config.PERSONA_FORMATS),
        lengths=_listing(config.PERSONA_LENGTHS),
        limits=", ".join(f"{item['code']} ({item['label'].lower()})" for item in config.PERSONA_LIMITS),
    )


def router_input(persona: "Persona | None") -> str:
    """Текущий профиль в том виде, в каком его видит маршрутизатор."""
    if persona is None:
        return ""
    return ROUTER_INPUT.format(profile=persona.text())


def parse_update(raw: dict | None) -> dict | None:
    """Вынуть из разобранного ответа маршрутизатора часть про профиль.

    Возвращает None, если про профиль в ответе ничего нет: «не упомянул» — это
    «не менять», а не «сбросить». Проверку значений делает `merge`.
    """
    if not isinstance(raw, dict):
        return None
    data = raw.get("persona")
    if not isinstance(data, dict) or not data:
        return None
    return data


def merge(
    persona: Persona,
    data: dict,
    turn: int = 0,
    source: str = "router",
    locked: frozenset = frozenset(),
) -> tuple[list[Change], list[str]]:
    """Применить к профилю то, что предложил маршрутизатор (или инструмент).

    В отличие от слоёв памяти, профиль приходит НЕ целиком: маршрутизатор
    возвращает только поля, которые просит изменить. Иначе любая мелочь в ответе
    переписывала бы профиль заново и стирала то, что человек выставил руками, —
    а профиль в первую очередь его, а не модели.

    `locked` — поля, которые агент уже правил инструментом на этом же обращении
    (см. `locked_by`): их совет маршрутизатора не перебивает.

    Значения вне реестра не применяются: они уходят в отказы и видны в трассе.
    """
    changes: list[Change] = []
    rejected: list[str] = []

    for field_name, registry, label in (
        ("style", config.STYLE_BY_CODE, "стиль"),
        ("format", config.FORMAT_BY_CODE, "формат"),
        ("length", config.LENGTH_BY_CODE, "длина"),
    ):
        wanted = _clean(data.get(field_name), 40)
        if not wanted or field_name in locked:
            continue
        if wanted not in registry:
            rejected.append(
                f"{label} «{wanted}» не из реестра — оставлен «{registry[getattr(persona, field_name)]['label']}»"
                if getattr(persona, field_name) in registry else f"{label} «{wanted}» не из реестра"
            )
            continue
        current = getattr(persona, field_name)
        if wanted == current:
            continue
        setattr(persona, field_name, wanted)
        changes.append(Change(
            field=field_name, source=source,
            what=f"{label}: {registry[current]['label'].lower()} → {registry[wanted]['label'].lower()}"
            if current in registry else f"{label}: {registry[wanted]['label'].lower()}",
        ))

    for item in _as_list(data.get("limits")):
        value = limit_code(_clean(item, 120))
        if not value or value in persona.limits:
            continue
        if len(persona.limits) >= config.PERSONA_LIMITS_MAX:
            rejected.append(f"ограничений уже {config.PERSONA_LIMITS_MAX} — «{value}» не добавлено")
            break
        persona.limits.append(value)
        changes.append(Change(field="limits", action="add", source=source,
                              what=f"ограничение: {config.limit_label(value).lower()}"))

    prefs = data.get("prefs")
    if isinstance(prefs, dict):
        for key, value in prefs.items():
            key, value = _clean(key, 40), _clean(value, 160)
            if not key or not value or f"pref:{key.lower()}" in locked:
                continue
            existing = next((p for p in persona.prefs if p.key.lower() == key.lower()), None)
            if existing is not None:
                if existing.value == value:
                    continue
                existing.value, existing.source, existing.turn = value, source, turn
                changes.append(Change(field="prefs", source=source, key=key,
                                      what=f"{key}: {value}"))
                continue
            if len(persona.prefs) >= config.PERSONA_PREFS_LIMIT:
                rejected.append(f"предпочтений уже {config.PERSONA_PREFS_LIMIT} — «{key}» не добавлено")
                break
            persona.prefs.append(Pref(key=key, value=value, source=source, turn=turn))
            changes.append(Change(field="prefs", action="add", source=source, key=key,
                                  what=f"{key}: {value}"))

    if changes:
        persona.updated_at = time.time()
    return changes, rejected


def apply_values(persona: Persona, values: dict) -> list[Change]:
    """Применить правку из окна: человек меняет профиль руками.

    Здесь же проверка: незнакомый код — ошибка (`ValueError`), а не тихая подмена.
    Человек должен видеть отказ так же, как его видит модель.
    """
    changes: list[Change] = []
    name = _clean(values.get("name"), 40)
    if name and name != persona.name:
        changes.append(Change(field="name", source="user", what=f"название: {name}"))
        persona.name = name
    about = _clean(values.get("about"), config.PERSONA_ABOUT_MAX)
    if about != persona.about:
        changes.append(Change(field="about", source="user",
                              what="кто вы: " + (about[:60] or "стёрто")))
        persona.about = about

    for field_name, registry, label in (
        ("style", config.STYLE_BY_CODE, "стиль"),
        ("format", config.FORMAT_BY_CODE, "формат"),
        ("length", config.LENGTH_BY_CODE, "длина"),
    ):
        wanted = _clean(values.get(field_name), 40)
        if not wanted:
            continue
        if wanted not in registry:
            raise ValueError(
                f"{label.capitalize()} «{wanted}» неизвестен. Доступны: "
                + ", ".join(registry) + "."
            )
        if wanted != getattr(persona, field_name):
            changes.append(Change(field=field_name, source="user",
                                  what=f"{label}: {registry[wanted]['label'].lower()}"))
            setattr(persona, field_name, wanted)

    if "limits" in values:
        limits: list[str] = []
        for item in _as_list(values.get("limits")):
            # Своё ограничение, написанное словами, приводим к коду реестра, если оно
            # про то же самое: иначе рядом с флажком появится его двойник текстом.
            code = limit_code(_clean(item, 120))
            if code and code not in limits:
                limits.append(code)
        limits = limits[:config.PERSONA_LIMITS_MAX]
        if limits != persona.limits:
            changes.append(Change(field="limits", source="user",
                                  what=f"ограничения: {len(limits)} шт."))
            persona.limits = limits

    if "prefs" in values:
        prefs = values.get("prefs") or {}
        items = prefs.items() if isinstance(prefs, dict) else [
            (item.get("key"), item.get("value")) for item in _as_list(prefs)
        ]
        fresh: list[Pref] = []
        for key, value in items:
            key, value = _clean(key, 40), _clean(value, 160)
            if not key or not value:
                continue
            was = next((p for p in persona.prefs if p.key.lower() == key.lower()), None)
            # Источник сохраняем, пока значение не изменилось: правка одного
            # предпочтения не должна приписывать человеку всё, что нашёл агент.
            keep = was is not None and was.value == value
            fresh.append(Pref(key=key, value=value, source=was.source if keep else "user",
                              turn=was.turn if keep else 0, at=was.at if keep else time.time()))
            if len(fresh) >= config.PERSONA_PREFS_LIMIT:
                break
        if [(p.key, p.value) for p in fresh] != [(p.key, p.value) for p in persona.prefs]:
            changes.append(Change(field="prefs", source="user",
                                  what=f"предпочтения: {len(fresh)} шт."))
            persona.prefs = fresh

    if changes:
        persona.updated_at = time.time()
    return changes


# --- Проверка готового ответа ------------------------------------------------

def check(answer: str, persona: "Persona | None") -> list[Check]:
    """Сверить ответ с профилем: длина, формат и проверяемые ограничения.

    Проверки нарочно простые и объяснимые — счёт слов и поиск разметки, никакой
    семантики. Семантическая проверка означала бы ещё одну модель, которой тоже
    надо верить, а выглядела бы как гарантия. Ответ при нарушении не
    перегенерируется: агент показывает факт, решать человеку.
    """
    if persona is None or not (answer or "").strip():
        return []
    checks = [_check_length(answer, persona), _check_format(answer, persona)]
    for code in persona.checked_limits():
        result = _check_limit(answer, config.LIMIT_BY_CODE[code])
        if result is not None:
            checks.append(result)
    return checks


def _check_length(answer: str, persona: Persona) -> Check:
    """Длина в словах: единственная величина профиля, измеряемая точно."""
    limit = config.length_words(persona.length)
    words = len(_WORD.findall(answer))
    # Допуск в четверть: потолок уходит в промпт словом «примерно», и придираться
    # к 75 словам вместо 70 значило бы считать нарушением то, о чём не просили.
    room = round(limit * 1.25)
    return Check(
        label=f"длина ≈ {limit} слов ({config.length_label(persona.length).lower()})",
        ok=words <= room,
        detail=f"в ответе {words} слов" + ("" if words <= room else f", допустимо до {room}"),
    )


def _check_format(answer: str, persona: Persona) -> Check:
    """Формат: список, нумерованные шаги или связный текст — по разметке ответа."""
    item = config.FORMAT_BY_CODE.get(persona.format)
    kind = item["check"] if item else ""
    listed = len(_LIST_LINE.findall(answer))
    numbered = len(_NUMBERED_LINE.findall(answer))
    label = f"формат: {config.format_label(persona.format).lower()}"
    if kind == "list":
        return Check(label=label, ok=listed >= 2, detail=f"пунктов списка: {listed}")
    if kind == "numbered":
        return Check(label=label, ok=numbered >= 2, detail=f"нумерованных шагов: {numbered}")
    return Check(label=label, ok=listed < 2,
                 detail="списков нет" if listed < 2 else f"в ответе список из {listed} пунктов")


def _check_limit(answer: str, item: dict) -> "Check | None":
    """Одно ограничение из реестра: код знает, по какому признаку его искать."""
    kind = item.get("check")
    label = f"ограничение: {item['label'].lower()}"
    if kind == "code":
        found = "```" in answer
        return Check(label=label, ok=not found,
                     detail="блоков кода нет" if not found else "в ответе есть блок кода")
    if kind == "emoji":
        found = _EMOJI.findall(answer)
        return Check(label=label, ok=not found,
                     detail="эмодзи нет" if not found else f"эмодзи: {' '.join(found[:5])}")
    if kind == "question":
        found = answer.count("?")
        return Check(label=label, ok=not found,
                     detail="вопросов нет" if not found else f"знаков вопроса: {found}")
    if kind == "words":
        hits = _found_words(answer, item.get("words") or ())
        return Check(label=label, ok=not hits,
                     detail="таких слов нет" if not hits else "найдено: " + ", ".join(hits))
    return None


def _found_words(answer: str, roots: tuple) -> list[str]:
    """Найти корни из списка, не считая строк, где агент объясняет отступление."""
    lines = [line for line in answer.splitlines()
             if not any(marker in line.lower() for marker in _EXPLAIN_MARKERS)]
    text = "\n".join(lines).lower()
    return [root for root in roots if re.search(rf"(?<![а-яёa-z]){re.escape(root)}", text)]


# --- Инструмент: агент правит профиль сам ------------------------------------
# Третий источник правок, и единственный, где решение принимает сам агент по ходу
# работы. Схема объявлена здесь, а исполняет её агент (`_persona_tool`): обычные
# инструменты — чистые функции, а этот меняет профиль его собеседника.

PREFER = {
    "type": "function",
    "function": {
        "name": "prefer",
        "description": (
            "Записать в профиль собеседника его предпочтение по ФОРМЕ ответа. "
            "field=\"style\" — стиль общения, field=\"format\" — формат ответа, "
            "field=\"length\" — длина ответа, field=\"limit\" — ограничение («без кода»), "
            "field=\"preference\" — своё предпочтение парой ключ-значение (нужен key). "
            "ВЫЗЫВАЙ СРАЗУ, ещё до ответа, как только прозвучало «запиши в профиль», «отвечай "
            "короче», «давай списком», «объясняй проще», «мне нужны примеры из…» — и только "
            "потом отвечай, сказав, что записал. Не говори «записал», не вызвав prefer: без "
            "вызова в профиле ничего не изменится. Факты о человеке (имя, занятие) сюда не "
            "клади — для них есть remember(layer=\"long\", kind=\"profile\")."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string",
                          "enum": ["style", "format", "length", "limit", "preference"],
                          "description": "Что именно правим в профиле"},
                "value": {"type": "string",
                          "description": "Значение: код из реестра для style/format/length, "
                                         "короткая фраза для limit и preference"},
                "key": {"type": "string",
                        "description": "Ключ предпочтения: одно-два слова НА ЯЗЫКЕ РАЗГОВОРА "
                                       "(«примеры», «единицы»); только для field=preference"},
            },
            "required": ["field", "value"],
        },
    },
}

TOOL_SPECS = [PREFER]
TOOL_NAMES = {spec["function"]["name"] for spec in TOOL_SPECS}
TOOL_TITLES = {"prefer": "правка профиля"}

# Без этой строки модель видит схему, но не понимает, зачем ей трогать профиль:
# она и так «старается угодить» в пределах одного ответа.
TOOLS_NOTE = (
    "\n\nПрофиль собеседника ты правишь сам: услышал просьбу отвечать иначе — сначала вызови "
    "prefer(field=…, value=…), потом отвечай. Просьбы «покороче», «списком», «без кода», "
    "«объясняй проще», «примеры из…» закрепляй в профиле всегда: без этого они забудутся "
    "вместе с окном диалога, а сказать «записал», не вызвав prefer, — значит обмануть."
)


def tool_specs(enabled: bool) -> list[dict]:
    """Схема инструмента, когда профиль подключён (выключенного не обещаем)."""
    return list(TOOL_SPECS) if enabled else []


def tool_catalog(enabled: bool) -> list[dict]:
    """Инструмент для каталога в интерфейсе — в том же виде, что остальные."""
    return [{"name": spec["function"]["name"],
             "title": TOOL_TITLES.get(spec["function"]["name"], spec["function"]["name"]),
             "description": spec["function"]["description"]}
            for spec in tool_specs(enabled)]


def apply_prefer(arguments: dict, persona: "Persona | None", turn: int = 0) -> tuple[str, Change]:
    """Исполнить `prefer`: положить предпочтение в профиль и сказать, что вышло.

    Ошибку поднимаем `ValueError` — агент превратит её в обычный результат
    инструмента, и модель увидит, какие значения вообще допустимы.
    """
    if persona is None:
        raise ValueError("профиль пользователя не подключён — править нечего")
    field_name = str(arguments.get("field") or "").strip()
    value = _clean(arguments.get("value"), 160)
    if not value:
        raise ValueError("не указано значение (value)")

    if field_name in ("style", "format", "length"):
        changes, rejected = merge(persona, {field_name: value}, turn, source="tool")
        if rejected:
            registry = {"style": config.STYLE_BY_CODE, "format": config.FORMAT_BY_CODE,
                        "length": config.LENGTH_BY_CODE}[field_name]
            raise ValueError(f"«{value}» не из реестра; доступны: " + ", ".join(registry))
        if not changes:
            return f"В профиле уже стоит «{value}» — ничего не изменилось.", Change(
                field=field_name, action="keep", source="tool", what=f"{field_name}: {value}")
        return f"Профиль обновлён: {changes[0].what}.", changes[0]

    if field_name == "limit":
        changes, rejected = merge(persona, {"limits": [value]}, turn, source="tool")
        if rejected:
            raise ValueError(rejected[0])
        if not changes:
            return "Такое ограничение в профиле уже есть.", Change(
                field="limits", action="keep", source="tool", what=f"ограничение: {value}")
        return f"В профиль добавлено ограничение: {config.limit_label(value)}.", changes[0]

    if field_name == "preference":
        key = _clean(arguments.get("key"), 40)
        if not key:
            raise ValueError("для field=preference нужен key — короткий ключ предпочтения")
        changes, rejected = merge(persona, {"prefs": {key: value}}, turn, source="tool")
        if rejected:
            raise ValueError(rejected[0])
        if not changes:
            return "Такое предпочтение уже записано.", Change(
                field="prefs", action="keep", source="tool", key=key, what=f"{key}: {value}")
        return f"В профиль записано предпочтение «{key}: {value}».", changes[0]

    raise ValueError(
        f"неизвестное поле «{field_name}»; доступны: style, format, length, limit, preference"
    )


# --- Заготовки и мелочи ------------------------------------------------------

def presets() -> list[Persona]:
    """Профили по умолчанию — те, что появляются при первом запуске.

    Содержимое живёт в `config.PERSONA_PRESETS`: набор профилей человек правит под
    себя, и ради этого лезть в код не нужно.
    """
    return [
        Persona(
            id=item["id"],
            name=item["name"],
            about=item.get("about", ""),
            style=item.get("style", config.PERSONA_STYLE),
            format=item.get("format", config.PERSONA_FORMAT),
            length=item.get("length", config.PERSONA_LENGTH),
            limits=list(item.get("limits") or []),
            prefs=[Pref(key=key, value=value, source="user")
                   for key, value in (item.get("prefs") or {}).items()],
        )
        for item in config.PERSONA_PRESETS
    ]


_DENIAL = ("без ", "не ", "никогда", "запрещ", "нельзя")


def limit_code(value: str) -> str:
    """Свободная фраза → код реестра, если она про то же самое.

    Иначе в профиле заводятся два ограничения об одном: агент записывает «Без кода
    в ответах» инструментом, а маршрутизатор следом — `no_code`, и одно из них
    проверяется кодом, а второе остаётся просьбой (поймано живой пробой). Правило
    нарочно грубое и объяснимое: отрицание плюс слово-примета из реестра.
    """
    text = (value or "").strip().lower()
    if text in config.LIMIT_BY_CODE:
        return text
    for item in config.PERSONA_LIMITS:
        if text == item["label"].lower():
            return item["code"]
        if any(mark in text for mark in _DENIAL) and any(
            word in text for word in item.get("match", ())
        ):
            return item["code"]
    return value


def _known(value: object, registry: dict, fallback: str) -> str:
    """Код из реестра или значение по умолчанию: чужие данные не должны ронять окно."""
    code = str(value or "").strip()
    return code if code in registry else fallback


def _listing(items: list[dict]) -> str:
    """Перечень вариантов реестра для инструкции: «code (подпись)»."""
    return ", ".join(f"{item['code']} ({item['label'].lower()})" for item in items)


def _clean(value: object, limit: int) -> str:
    """Строку из чужих рук приводим к одной строке и режем по длине."""
    return " ".join(str(value or "").split())[:limit].strip()


def _as_list(value: object) -> list:
    """Список из того, что прислали: строка тоже считается списком из одного пункта."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []
