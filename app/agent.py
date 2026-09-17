"""Агент — самостоятельная сущность приложения.

Ключевая идея: агент — это не «один вызов API», а объект со своим паспортом
(имя, роль, инструкция), собственными настройками, собственной памятью и
собственными инструментами. Наружу он отдаёт один вход — `ask()`; весь цикл
работы спрятан внутри.

Что происходит в `ask()`:

    1. проверка и нормализация входа
    2. план: агент отдельным вызовом решает, нужен ли план, и пишет шаги
    3. бюджет контекста: агент считает вес будущего запроса в токенах и, если тот
       не помещается в окно модели, подрезает память — до вызова, а не после
    4. цикл работы: модель либо просит вызвать инструмент, либо даёт ответ.
       Инструмент исполняет агент, результат возвращается в диалог, цикл идёт
       дальше — до финального ответа или до потолка шагов
    5. разбор ответа и сверка оценки токенов с фактическим `usage`
    6. запись в память: сообщения ложатся в краткосрочный слой, маршрутизатор
       раскладывает новое по рабочему и долговременному слоям, суммаризация
       сворачивает выпавшее из окна — и всё это уходит на диск одной записью

Именно шаг 4 отличает агента от чата: одна фраза пользователя разворачивается в
последовательность действий, которые агент выбирает и выполняет сам.

Токены агент считает сам и до отправки (`app/tokens.py`): контекст — ресурс с
жёстким потолком, и знать его цену постфактум поздно. Оценка сверяется с
фактическим `usage` из ответа, расхождение уходит в калибровку, а расход каждого
обращения ложится в хранилище — по нему видно, как дорожает разговор.

Память у агента разложена по слоям, и модель этих слоёв описана отдельно — в
`app/memory.py`. Здесь она живёт тремя полями и тремя блоками инструкции:

    краткосрочная  `_memory` — окно контекста: последние `memory_turns` пар
                   «вопрос-ответ» как есть, ровно то, что уходит в модель
                   дословно, плюс `_summary` — суммаризация выпавшего из окна;
    рабочая        `_task` — карточка текущей задачи: цель, шаги, находки,
                   созданные файлы, открытые вопросы. Включается настройкой
                   `working`; закрытая задача уходит в архив и из запроса;
    долговременная `_long` — записи «ключ — значение» трёх видов (профиль,
                   решения, знания). Включается стратегией `facts` и переживает
                   задачи, сжатие и перезапуск.

Что куда класть, решается в трёх местах, и каждое видно в трассе: правила в коде
(`memory.rules_from_turn`), отдельный вызов-маршрутизатор после ответа (`_route`)
и инструменты `remember` / `recall`, которыми агент кладёт в слой сам.

Поверх памяти надет профиль пользователя (`app/persona.py`) — четвёртый блок
инструкции и ответ на другой вопрос: не «что агент помнит», а «для кого он
говорит». Профиль задаёт стиль, формат и ограничения ответа, подключается к
КАЖДОМУ запросу и переключается на лету: один и тот же вопрос при разных профилях
получает разные ответы. Наполняется он так же в три руки — человек правит в окне,
маршрутизатор замечает просьбы в разговоре, агент кладёт инструментом `prefer`, —
а после генерации ответ сверяется с профилем (`persona.check`), и расхождения
видно под ответом.

Что делать с тем, что выпало из окна краткосрочной памяти, решает стратегия
контекста (`strategy`):

    window    скользящее окно (Sliding Window) — выпавшее отбрасывается;
    facts     факты (Sticky Facts / Key-Value Memory) — включён долговременный
              слой, и в запрос уходят его записи + окно;
    branches  ветки диалога (Branching): точка ветвления фиксирует место, от неё
              создаются ветки, каждая продолжается независимо; в модель уходит
              окно активной ветки.

Поверх любой из них включается сжатие истории (`summarize`) — это не стратегия, а
опция: выпавшее из окна копится в `_pending_summary` и каждые `summary_every`
сообщений сворачивается в суммаризацию (`_summary`), которая уходит в запрос
вместо самих сообщений. Поэтому сочетания работают вместе: «факты + суммаризация»,
«ветки + суммаризация» и так далее.

Полная переписка каждой ветки вместе с суммаризациями, задачами, долговременной
памятью, паспортом и настройками пишется в хранилище (`store.py`), поэтому агент
не начинает с нуля после перезапуска приложения: `load_agents()` поднимает тех же
агентов со всеми их слоями, и разговор продолжается так, будто его не прерывали.

Всё вокруг агента намеренно «глупое»: `llm.py` умеет только сходить в HTTP API
модели, `tools.py` — только выполнить работу, `store.py` — только положить
состояние на диск, а интерфейс — только показать ввод, вывод и трассу шагов. Ни
один из них не знает, как формируется запрос и что происходит с ответом, поэтому
агента можно поднять в любом окружении: окне, консоли, сервере или тесте.
"""
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from . import config, llm, memory, persona, tokens, tools
from .store import MAIN_BRANCH, Store

logger = logging.getLogger("app.agent")

# «Свой профиль» для сборки инструкции. Нужен, потому что None здесь значит не
# «по умолчанию», а «без профиля вообще»: теневой прогон умеет сравнивать ответ и
# с другим профилем, и с полным его отсутствием.
_SELF = object()


class AgentError(Exception):
    """Ошибка на стороне агента: плохой вход, неверная настройка или сбой вызова.

    Интерфейсы ловят только её и показывают текст пользователю — им не нужно
    знать про устройство API модели.
    """


@dataclass(frozen=True)
class AgentProfile:
    """Паспорт агента: кто он и как себя ведёт.

    `instructions` уходит в модель system-сообщением, `name` и `role` нужны
    интерфейсу. Понадобится другой агент — меняется здесь, остальной код тот же.
    """
    name: str
    role: str
    instructions: str


DEFAULT_PROFILE = AgentProfile(
    name="Адвент",
    role="ИИ-ассистент по курсу",
    instructions=(
        "Ты — «Адвент», агент участника курса по разработке ИИ-агентов. "
        "Отвечай по-русски, по делу и без воды. Если вопрос неоднозначный — задай "
        "один уточняющий вопрос вместо догадок. Если чего-то не знаешь, прямо скажи "
        "об этом и не выдумывай факты. Учитывай, о чём шла речь раньше в диалоге."
    ),
)

# Инструкция про инструменты добавляется к роли, только когда инструменты включены:
# если их выключили, обещать модели несуществующие возможности нельзя.
TOOLS_NOTE = (
    "\n\nУ тебя есть инструменты, и ты умеешь действовать, а не только говорить. "
    "Никогда не выдумывай то, что можно получить инструментом: текущее время, "
    "результат вычисления, содержимое файлов рабочей папки, текст страницы по адресу, "
    "погоду. Работай шагами: вызови инструмент, посмотри результат, реши, что дальше. "
    "Если инструмент вернул ошибку — прочитай её и попробуй иначе. "
    "Когда задача выполнена, дай короткий финальный ответ и перечисли, что именно "
    "сделал."
)

# Инструменты памяти уходят в модель вместе с остальными, но сами по себе они
# ничего не объясняют: схему модель видит, а зачем ей что-то класть в память, если
# всё и так в контексте, — нет. Эта заметка добавляется, только когда включён хотя
# бы один из слоёв, которыми можно управлять.
MEMORY_TOOLS_NOTE = memory.TOOLS_NOTE

# Про память модели надо сказать прямо. Переписка уходит в запрос, но роль агента
# пишет пользователь, и без этой заметки модель отвечает выученным «я не помню
# прошлые разговоры» — хотя весь разговор лежит у неё же в контексте.
MEMORY_NOTE = (
    "\n\nДальше в этом диалоге идёт твоя память: {count} сообщ. прошлого разговора "
    "(последнее — {when}). Память хранится на диске и восстанавливается при запуске "
    "приложения, так что разговор продолжается, даже если его прерывали. Ты "
    "действительно помнишь всё, что в ней есть: спросят, о чём говорили раньше — "
    "отвечай по этой переписке и никогда не заявляй, что не помнишь прошлые разговоры "
    "или что каждый диалог начинается с чистого листа."
)

# Начало разговора: памяти нет, и придумывать «прошлые беседы» тоже нельзя.
NO_MEMORY_NOTE = (
    "\n\nЭто начало разговора: прошлых сообщений в памяти нет. Если спросят, о чём "
    "говорили раньше, честно скажи, что разговор только начался."
)

# Суммаризация — начало разговора, свёрнутое в список фактов. Модели надо сказать, что
# это именно её память, а не чужой текст, и что подробности в ней могли потеряться:
# иначе она либо игнорирует суммаризацию, либо уверенно «вспоминает» то, чего в ней нет.
SUMMARY_NOTE = (
    "\n\nРазговор длинный, поэтому его начало ({count} сообщ.) заменено суммаризацией — это "
    "твоя память о той части разговора. Опирайся на суммаризацию как на факты, но помни, что "
    "подробности в ней могли потеряться: если спросят о том, чего в ней нет, честно скажи, "
    "что такая деталь не сохранилась.\nСуммаризация прошлого разговора:\n{summary}"
)

# Роль для вызова, который обновляет суммаризацию. Прежняя суммаризация подаётся на вход,
# поэтому результат — суммаризация всего разговора, а не только последних сообщений.
SUMMARY_SYSTEM = (
    "Ты составляешь суммаризацию долгого разговора между пользователем и агентом «{name}». Тебе "
    "дают прежнюю суммаризацию и новые сообщения, которые уходят из памяти агента. Верни "
    "обновлённую суммаризацию целиком: прежние факты, которые ещё важны, плюс новое.\n"
    "Обязательно сохраняй: как зовут пользователя и чем он занимается, его предпочтения и "
    "просьбы, договорённости и решения, числа, даты, названия файлов и тем, что агент уже "
    "сделал (в том числе инструментами) и что осталось открытым. Убирай вежливость, "
    "повторы и пересказ общеизвестного.\n"
    "Формат: список коротких пунктов, каждый — одна строка, не больше {points} пунктов. "
    "Пиши по-русски, только факты из сообщений, ничего не выдумывай. Верни только "
    "суммаризацию, без заголовков и пояснений."
)

SUMMARY_USER = "Прежняя суммаризация:\n{summary}\n\nНовые сообщения ({count}):\n{messages}"

PLANNER_SYSTEM = (
    "Ты — планировщик агента. По задаче пользователя реши, нужен ли план действий.\n"
    "Инструменты, доступные исполнителю:\n{tools}\n\n"
    "Если задача решается одним ответом без действий (вопрос, объяснение, беседа) — "
    "верни пустой список шагов. Если нужны действия — 2–5 коротких шагов в "
    "повелительном наклонении, каждый шаг — одно действие, по-русски.\n"
    "У исполнителя есть память прошлого разговора, ты её не видишь: вопросы вроде "
    "«о чём мы говорили раньше» он решает сам, без действий — на них возвращай "
    "пустой список шагов.\n"
    'Верни СТРОГО JSON без пояснений и markdown: {{"steps": ["...", "..."]}}'
)

# Когда шаги закончились, а модель всё ещё зовёт инструменты — просим подвести итог.
FINISH_NUDGE = (
    "Лимит шагов исчерпан. Больше инструменты не вызывай: дай финальный ответ по "
    "тому, что уже сделано, и честно скажи, если что-то осталось невыполненным."
)

# Потолок глубины памяти. Ограничение не техническое, а денежное: окно контекста у
# моделей огромное, но каждая пара из памяти уезжает в модель заново при каждом
# обращении — сто пар в памяти означают сто пар в каждом счёте.
MEMORY_TURNS_MAX = 200

# Суммаризация обновляется, когда за окном памяти накопилось столько сообщений. Меньше
# двух не бывает — сообщения ходят парами; больше сотни бессмысленно — такая очередь
# сама по себе весит как хороший запрос.
SUMMARY_EVERY_MIN, SUMMARY_EVERY_MAX = 2, 100

# Имя ветки или точки ветвления — короткая подпись для вкладки.
NAME_MAX = 40
MAIN_BRANCH_NAME = "основная"

# По этим словам в ответе провайдера видно, что запрос отклонён именно по длине.
# Такую ошибку агент переводит на человеческий язык: «Модель не ответила: 400 …»
# ничего не объясняет, а «в запрос ушло 1.2M токенов при окне 1M» — объясняет.
LENGTH_ERROR_MARKERS = (
    "input length", "range of input", "context length", "maximum context",
    "too long", "exceeds", "token limit",
)


@dataclass
class AgentStep:
    """Один выполненный шаг: какой инструмент вызвал агент и что получил."""
    number: int
    tool: str
    title: str
    arguments: dict
    result: str
    ok: bool = True
    elapsed_s: float | None = None

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "tool": self.tool,
            "title": self.title,
            "arguments": self.arguments,
            "result": self.result,
            "ok": self.ok,
            "elapsed_s": self.elapsed_s,
        }


@dataclass
class _Totals:
    """Счётчик по всему обращению: у агента вызовов модели теперь несколько."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    has_cost: bool = False
    llm_calls: int = 0

    def add(self, raw: dict) -> dict:
        usage = raw.get("usage")
        if usage:
            self.prompt_tokens += usage["prompt_tokens"]
            self.completion_tokens += usage["completion_tokens"]
            self.total_tokens += usage["total_tokens"]
        if raw.get("cost_usd") is not None:
            self.cost_usd += raw["cost_usd"]
            self.has_cost = True
        self.llm_calls += 1
        return raw

    def usage(self) -> dict | None:
        if not self.total_tokens:
            return None
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class Summary:
    """Суммаризация: начало разговора, свёрнутое в короткий список фактов.

    Живёт отдельно от истории: сообщения в базе остаются как были, а в запрос
    вместо них уходит этот текст. `upto` — граница в истории: всё, что не новее
    этого сообщения, уже в суммаризации и в окно памяти больше не возвращается.
    """
    text: str = ""
    version: int = 0          # сколько раз суммаризация обновлялась
    upto: int = 0             # id последнего сообщения истории, вошедшего в суммаризацию
    messages: int = 0         # сколько сообщений она заменяет
    tokens: int = 0           # сколько они весили бы в запросе (оценка)
    at: float | None = None   # когда обновлён

    def __bool__(self) -> bool:
        return bool(self.text)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "version": self.version,
            "upto": self.upto,
            "messages": self.messages,
            "tokens": self.tokens,
            "at": self.at,
        }


@dataclass
class Compression:
    """Что произошло при сжатии: сколько сообщений свёрнуто и во что это обошлось."""
    messages: int            # сколько сообщений ушло в суммаризацию этим разом
    before: int              # сколько они весили в токенах (оценка)
    after: int               # сколько весит обновлённая суммаризация целиком
    version: int
    text: str                # сама суммаризация
    elapsed_s: float
    call_tokens: int         # во что обошёлся вызов, который его составил

    def to_dict(self) -> dict:
        return {
            "messages": self.messages,
            "before": self.before,
            "after": self.after,
            "version": self.version,
            "text": self.text,
            "elapsed_s": self.elapsed_s,
            "call_tokens": self.call_tokens,
        }


@dataclass
class Shadow:
    """Теневой ответ для сравнения: тот же вопрос, но при других условиях.

    Считается и оплачивается по-настоящему, но в память не идёт: он нужен, только
    чтобы положить рядом два ответа и два счёта. Сравнивать можно двумя способами,
    и `kind` говорит, каким именно:

        history   вся история как есть вместо стратегии контекста (день 9);
        persona   тот же контекст, но другой профиль пользователя — так видно,
                  что персонализация меняет сам ответ, а не только его тон.
    """
    text: str
    messages: int             # сколько сообщений истории ушло в модель
    breakdown: tokens.Breakdown
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    elapsed_s: float
    trimmed_pairs: int = 0    # даже полная история не влезла в окно — что-то выброшено
    tool_calls: int = 0       # модель попросила инструменты: теневой прогон их не исполняет
    finish_reason: str | None = None
    kind: str = "history"     # с чем сравниваем: history / persona
    label: str = ""           # подпись сравнения: «профиль «Новичок»»
    checks: list = field(default_factory=list)   # соблюдён ли тот профиль (persona.Check)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "kind": self.kind,
            "label": self.label,
            "checks": [check.to_dict() for check in self.checks],
            "messages": self.messages,
            "breakdown": self.breakdown.to_dict(),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
            "elapsed_s": self.elapsed_s,
            "trimmed_pairs": self.trimmed_pairs,
            "tool_calls": self.tool_calls,
            "finish_reason": self.finish_reason,
        }


@dataclass
class TokenReport:
    """Счёт за обращение в токенах: оценка до вызова и факт после.

    Оценку агент считает сам (`app/tokens.py`) ещё до того, как запрос ушёл в
    сеть, — иначе о цене и о переполнении контекста узнаёшь только по факту, то
    есть когда платить уже поздно. Факт приходит в `usage`, и разница между
    оценкой и фактом здесь же: по ней видно, можно ли счётчику верить.

    Здесь же — цена слоёв памяти: сколько весит каждый из них в этом запросе,
    сколько сообщений истории в модель не ушло дословно, чем их заменили
    (суммаризация, долговременная память или ничем) и сколько весил бы весь
    запрос, уйди история как есть.
    """
    breakdown: tokens.Breakdown = field(default_factory=tokens.Breakdown)
    estimated: int = 0            # оценка запроса до отправки
    prompt_tokens: int = 0        # факт по тому же запросу
    completion_tokens: int = 0    # факт по итоговому ответу
    total_prompt: int = 0         # факт по всем вызовам обращения (план, шаги, итог)
    total_completion: int = 0
    limit: int = 0                # окно контекста модели
    reserve: int = 0              # запас, оставленный под ответ
    max_output: int = 0           # потолок генерации у модели
    trimmed_pairs: int = 0        # сколько пар памяти выкинуто, чтобы влезть в окно
    truncated: bool = False       # ответ упёрся в лимит генерации и оборван
    strategy: str = ""            # стратегия контекста в этом обращении
    summarize: bool = False       # было ли включено сжатие истории
    branch: int = MAIN_BRANCH     # в какой ветке шёл разговор
    summary_version: int = 0      # какая суммаризация ушла в запрос (0 — без суммаризации)
    folded_messages: int = 0      # сколько сообщений она заменила
    folded_tokens: int = 0        # сколько они весили бы сами (оценка)
    working: bool = False         # был ли включён слой рабочей памяти
    long_version: int = 0         # какая версия долговременной памяти ушла в запрос (0 — слой выключен)
    long_items: int = 0           # сколько в ней записей
    task_items: int = 0           # сколько пунктов в карточке задачи (0 — задачи нет)
    task_title: str = ""          # название открытой задачи
    persona_id: str = ""          # какой профиль пользователя был подключён
    persona_name: str = ""        # его название — для строки под ответом
    persona_items: int = 0        # сколько в нём пунктов (0 — профиль не подключён)
    persona_summary: str = ""     # стиль · формат · длина одной строкой
    task_state: str = ""          # этап автомата, на котором шло обращение
    task_step: int = 0            # номер текущего шага плана
    task_total: int = 0           # всего шагов в плане
    task_expect: str = ""         # ожидаемое действие: чей ход и чего ждут
    task_paused: bool = False     # задача отложена: вместо карточки ушла закладка
    task_resumed: bool = False    # это первый ответ после паузы
    pending_messages: int = 0     # ждут суммаризации
    pending_tokens: int = 0
    route_pending: int = 0        # ждут разбора маршрутизатором
    dropped_messages: int = 0     # сообщений истории, не ушедших в модель дословно и не свёрнутых
    dropped_tokens: int = 0       # сколько они весили бы в запросе

    @property
    def context_used(self) -> int:
        """Сколько токенов реально заняло окно контекста (факт, пока его нет — оценка)."""
        return self.prompt_tokens or self.estimated

    @property
    def fill(self) -> float:
        """Доля окна контекста, занятая запросом."""
        return self.context_used / self.limit if self.limit else 0.0

    @property
    def error_pct(self) -> float | None:
        """На сколько процентов оценка разошлась с фактом (со знаком)."""
        if not self.prompt_tokens or not self.estimated:
            return None
        return round((self.estimated - self.prompt_tokens) / self.prompt_tokens * 100, 1)

    @property
    def uncompressed(self) -> int:
        """Сколько весил бы тот же запрос с полной историей как есть.

        Без блоков памяти, зато со всеми сообщениями, которые слои свернули или
        отбросили: это и есть «до» для сравнения со стратегией.
        """
        return (self.context_used - self.breakdown.summary - self.breakdown.long
                - self.breakdown.task + self.folded_tokens + self.dropped_tokens)

    @property
    def saved(self) -> int:
        """Сколько токенов сберегла стратегия в этом запросе.

        Может быть и меньше нуля: пока суммаризация или факты молоды, они бывают
        тяжелее тех нескольких сообщений, которые заменили, — окупаются на длине.
        """
        return self.uncompressed - self.context_used

    def to_dict(self) -> dict:
        return {
            "breakdown": self.breakdown.to_dict(),
            "estimated": self.estimated,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_prompt": self.total_prompt,
            "total_completion": self.total_completion,
            "limit": self.limit,
            "reserve": self.reserve,
            "max_output": self.max_output,
            "trimmed_pairs": self.trimmed_pairs,
            "truncated": self.truncated,
            "strategy": self.strategy,
            "summarize": self.summarize,
            "branch": self.branch,
            "summary_version": self.summary_version,
            "folded_messages": self.folded_messages,
            "folded_tokens": self.folded_tokens,
            "working": self.working,
            "long_version": self.long_version,
            "long_items": self.long_items,
            "task_items": self.task_items,
            "task_title": self.task_title,
            "persona_id": self.persona_id,
            "persona_name": self.persona_name,
            "persona_items": self.persona_items,
            "persona_summary": self.persona_summary,
            "task_state": self.task_state,
            "task_step": self.task_step,
            "task_total": self.task_total,
            "pending_messages": self.pending_messages,
            "pending_tokens": self.pending_tokens,
            "route_pending": self.route_pending,
            "dropped_messages": self.dropped_messages,
            "dropped_tokens": self.dropped_tokens,
            "uncompressed": self.uncompressed,
            "saved": self.saved,
            "context_used": self.context_used,
            "fill": self.fill,
            "error_pct": self.error_pct,
        }


@dataclass
class AgentReply:
    """Ответ агента: текст, трасса выполненных шагов и метрики всего обращения."""
    text: str
    model: str
    turn: int
    plan: list[str] = field(default_factory=list)
    steps: list[AgentStep] = field(default_factory=list)
    llm_calls: int = 1
    finish_reason: str | None = None
    usage: dict | None = None
    elapsed_s: float | None = None
    cost_usd: float | None = None
    temperature: float | None = None
    tier: str | None = None
    sent_messages: int = 0          # сколько сообщений ушло в модель в последнем вызове
    tokens: TokenReport | None = None   # счёт за обращение: оценка, факт и лимиты
    compression: Compression | None = None  # после ответа часть истории свёрнута в суммаризацию
    # Аннотация строкой: имя поля совпадает с именем модуля, и без кавычек Python
    # разобрал бы `memory.MemoryUpdate` уже по самому полю, а не по модулю.
    memory: "memory.MemoryUpdate | None" = None   # что и в какой слой памяти положено после ответа
    # Та же ловушка с именем поля, что и у `memory`: имя совпадает с именем модуля,
    # поэтому аннотация обязательно строкой.
    persona: "persona.PersonaUpdate | None" = None   # правки профиля и сверка ответа с ним
    violations: list[str] = field(default_factory=list)  # какие инварианты нарушил ответ
    shadow: Shadow | None = None    # теневой ответ «с полной историей» для сравнения
    request: dict | None = None     # «сырой обмен»: тело последнего запроса
    response: dict | None = None    # «сырой обмен»: ответ модели как есть

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "model": self.model,
            "model_label": config.model_label(self.model),
            "turn": self.turn,
            "plan": self.plan,
            "steps": [s.to_dict() for s in self.steps],
            "llm_calls": self.llm_calls,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "elapsed_s": self.elapsed_s,
            "cost_usd": self.cost_usd,
            "temperature": self.temperature,
            "tier": self.tier,
            "sent_messages": self.sent_messages,
            "tokens": self.tokens.to_dict() if self.tokens else None,
            "compression": self.compression.to_dict() if self.compression else None,
            "memory": self.memory.to_dict() if self.memory else None,
            "persona": self.persona.to_dict() if self.persona else None,
            "violations": list(self.violations),
            "shadow": self.shadow.to_dict() if self.shadow else None,
            "request": self.request,
            "response": self.response,
        }


def _silent(kind: str, data: dict) -> None:
    """Колбэк прогресса по умолчанию: агента никто не слушает — и хорошо."""


def _main_branch() -> dict:
    """Описание основной ветки: у неё нет строки в хранилище, она есть всегда."""
    return {"id": MAIN_BRANCH, "name": MAIN_BRANCH_NAME, "origin": "", "shared": 0, "fork_at": 0}


@dataclass
class Agent:
    """Экземпляр агента: паспорт + настройки + память + инструменты + счётчики.

    Память диалога хранится здесь, в самом агенте: интерфейс присылает только
    очередное сообщение, а контекст для модели агент собирает сам. Агентов может
    быть несколько — они независимы, память одного не видна другому.

    Если агенту дали хранилище (`store`), он сам записывает туда каждое обращение
    и каждую смену настроек — и переживает перезапуск приложения. Без хранилища
    агент полностью работоспособен, просто помнит разговор только до закрытия;
    без хранилища нет только веток и точек ветвления — они живут в истории.
    """
    profile: AgentProfile = DEFAULT_PROFILE
    model: str = config.DEFAULT_MODEL
    temperature: float = config.AGENT_TEMPERATURE
    max_tokens: int | None = config.AGENT_MAX_TOKENS
    memory_turns: int = config.AGENT_MEMORY_TURNS
    tools_enabled: bool = config.AGENT_TOOLS      # давать ли модели инструменты
    planning: bool = config.AGENT_PLANNING        # писать ли план перед работой
    max_steps: int = config.AGENT_MAX_STEPS       # потолок шагов цикла за обращение
    strategy: str = config.AGENT_STRATEGY         # стратегия контекста: window / facts / branches
    summarize: bool = config.AGENT_SUMMARIZE      # сжимать ли историю — опция поверх любой стратегии
    summary_every: int = config.AGENT_SUMMARY_EVERY  # сколько сообщений копить до обновления суммаризации
    working: bool = config.AGENT_WORKING          # вести ли слой рабочей памяти (карточку задачи)
    # Профиль пользователя — ссылкой, а не объектом: профили общие для всех агентов
    # и лежат отдельно от переписки, поэтому в настройках агента хранится только id
    # (пусто — работать без профиля), а сам профиль поднимается из хранилища.
    persona_id: str = config.AGENT_PERSONA

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: float = field(default_factory=time.time)
    turns: int = 0                                # сколько запросов агент обработал
    branch: int = MAIN_BRANCH                     # активная ветка диалога
    store: Store | None = field(default=None, repr=False)   # куда класть состояние
    restored: bool = False                        # поднят из истории, а не создан заново
    # Краткосрочный слой: окно контекста как есть плюс суммаризация выпавшего.
    _memory: list[dict] = field(default_factory=list, repr=False)
    _summary: Summary = field(default_factory=Summary, repr=False)   # свёрнутое начало разговора
    # Рабочий и долговременный слои (см. app/memory.py).
    _task: "memory.Task | None" = field(default=None, repr=False)    # карточка текущей задачи
    _long: "memory.LongTerm" = field(default_factory=memory.LongTerm, repr=False)  # профиль, решения, знания
    # Две очереди, потому что сжатие и маршрутизатор работают независимо и могут быть
    # включены одновременно: в первую попадает выпавшее из окна, во вторую — каждый
    # новый обмен, который маршрутизатор ещё не разложил по слоям.
    _pending_summary: list[dict] = field(default_factory=list, repr=False)  # ждут суммаризации
    _pending_route: list[dict] = field(default_factory=list, repr=False)    # ждут маршрутизации
    # Профиль пользователя: сам объект (см. app/persona.py). Аннотация строкой —
    # имя поля совпадает с именем модуля.
    _persona: "persona.Persona | None" = field(default=None, repr=False)
    # Что агент положил в слои инструментом за это обращение — копится по ходу цикла
    # и уходит в трассу вместе с решениями маршрутизатора.
    _tool_routes: list = field(default_factory=list, repr=False)
    # То же самое для профиля: правки этого обращения (инструментом и маршрутизатором)
    # и результат сверки ответа с профилем. Наполняется по ходу, как и `_live`.
    _persona_update: "persona.PersonaUpdate" = field(
        default_factory=persona.PersonaUpdate, repr=False)
    # Память обращения, которая наполняется по ходу: правила срабатывают сразу, а не
    # после ответа, и переходы автомата видно в момент, когда они происходят.
    _live: "memory.MemoryUpdate" = field(default_factory=memory.MemoryUpdate, repr=False)
    _notify: object = field(default=_silent, repr=False)   # колбэк прогресса (см. ask)
    _state_before: str = field(default=config.AGENT_TASK_STATE, repr=False)  # этап на начало обращения
    _resumed_before: bool = field(default=False, repr=False)  # обращение началось сразу после паузы
    _rule_moves: int = field(default=0, repr=False)  # переходов по правилу за текущее обращение
    _branch: dict = field(default_factory=_main_branch, repr=False)  # описание активной ветки

    def __post_init__(self) -> None:
        """Профиль пользователя поднимается сразу: он нужен уже первому запросу.

        Остальные слои читаются из истории и потому поднимаются в `restore`, а
        профиль лежит вне переписки — его можно взять и у только что созданного
        агента.
        """
        if self.store is not None and self._persona is None and self.persona_id:
            self._load_persona()

    # ------------------------------------------------------------------- вход --

    def ask(
        self,
        message: str,
        compare: bool = False,
        on_event=None,
        compare_with: "str | persona.Persona | None" = None,
    ) -> AgentReply:
        """Единственный публичный вход: принять запрос и вернуть ответ агента.

        `compare=True` — заодно получить теневой ответ на тот же вопрос, но с
        полной историей вместо стратегии контекста: два ответа и два счёта рядом.
        Настоящий ответ при этом один — тот, что идёт в память.

        `compare_with` — то же сравнение, но по другой оси: тот же вопрос и тот же
        контекст, а профиль пользователя другой. Принимает id профиля, сам профиль
        или пустую строку («ответить вообще без профиля»); None — не сравнивать.
        Это и есть проверка задания «ответы для разных профилей» в один приём.

        `on_event(kind, data)` — необязательный колбэк прогресса. Обращение может
        занять полминуты, и за это время агент успевает составить план, выполнить
        его шагами и провести задачу через два этапа автомата; без колбэка всё это
        всплывает разом в конце, и движения не видно. Про интерфейс агент при этом
        по-прежнему ничего не знает: он просто зовёт функцию, а что с ней делать —
        дело вызывающего.
        """
        text = self._prepare(message)                       # 1. проверка входа
        started = time.perf_counter()
        totals = _Totals()
        steps: list[AgentStep] = []
        self._tool_routes = []   # что агент положит в слои сам, инструментом
        self._persona_update = persona.PersonaUpdate(
            name=self._persona.name if self._persona else "",
        )
        self._notify = on_event if callable(on_event) else _silent
        # Этап на начало обращения: маршрутизатор увидит именно его и, «оставляя всё
        # как есть», вернёт то же значение. Если к тому моменту код уже сдвинул этап
        # по факту работы, такой ответ — не просьба вернуться назад (см. _call_router).
        self._state_before = self._task.state if self._task else config.AGENT_TASK_STATE
        # Первый ответ после паузы: пометку в инструкции снимет `_route`, когда она
        # отработает, — а решение об этом принимается здесь, до сборки запроса.
        self._resumed_before = bool(self._task is not None and self._task.resuming)
        self._rule_moves = 0
        # Всё, что случилось с памятью по ходу обращения, копится здесь и достаётся
        # маршрутизатору уже заполненным: правила срабатывают не в конце, а сразу.
        self._live = memory.MemoryUpdate()

        logger.info(
            "Агент «%s» [%s] ← запрос #%d (%d симв.) · инструменты=%s · план=%s · стратегия=%s · "
            "сжатие=%s · рабочая память=%s · ветка=%s",
            self.profile.name, self.id, self.turns + 1, len(text), self.tools_enabled, self.planning,
            self.strategy, self.summarize, self.working, self._branch["name"],
        )

        plan = self._make_plan(text, totals)                # 2. план действий
        if plan:
            self._notify("plan", {"steps": list(plan)})
        # План — это уже задача: заводим карточку сразу, а не после ответа, иначе
        # рабочая память наполняется задним числом и движения этапов не видно.
        self._live_rules(plan, [])
        specs = self._specs()
        prompt = self._system_prompt(plan)
        summary_block = self._summary_block()               #    блоки слоёв внутри инструкции
        long_block = self._long_block() + self._invariants_block()
        task_block = self._task_block()
        persona_block = self._persona_block()               #    профиль пользователя — тоже блок
        window, dropped = self._fit_context(prompt, text, specs)   # 3. бюджет контекста
        beyond, beyond_tokens = self._beyond_window()
        report = TokenReport(
            breakdown=tokens.measure(prompt, window, text, specs, self.model,
                                     summary=summary_block, long=long_block, task=task_block,
                                     persona=persona_block),
            limit=self._context_limit(),
            reserve=self._answer_reserve(),
            max_output=config.model_max_output(self.model),
            trimmed_pairs=dropped,
            strategy=self.strategy,
            summarize=self.summarize,
            branch=self.branch,
            summary_version=self._summary.version if summary_block else 0,
            folded_messages=self._summary.messages if summary_block else 0,
            folded_tokens=self._summary.tokens if summary_block else 0,
            working=self.working,
            long_version=self._long.version if long_block else 0,
            long_items=len(self._long.notes) if long_block else 0,
            # На паузе в запрос ушла закладка, а не карточка: пунктов в нём ноль,
            # хотя сами пункты никуда не делись и вернутся вместе с задачей.
            task_items=self._task.size() if task_block and not self._task.paused else 0,
            task_title=self._task.title if task_block else "",
            persona_id=self._persona.id if self._persona else "",
            persona_name=self._persona.name if self._persona else "",
            persona_items=self._persona.size() if self._persona else 0,
            persona_summary=self._persona.summary() if self._persona else "",
            task_state=self._task.state if task_block else "",
            task_step=self._task.step if task_block else 0,
            task_total=self._task.total if task_block else 0,
            task_expect=self._task.expect_line() if task_block else "",
            task_paused=bool(task_block and self._task.paused),
            task_resumed=self._resumed_before,
            pending_messages=len(self._pending_summary),
            pending_tokens=tokens.measure_messages(self._pending_summary, self.model),
            route_pending=len(self._pending_route),
            dropped_messages=beyond,
            dropped_tokens=beyond_tokens,
        )
        report.estimated = report.breakdown.total
        messages = self._build_messages(prompt, window, text)      #    сборка запроса
        logger.info(
            "Агент «%s» [%s]: в запрос уйдёт ≈%d токенов (инструкция %d + профиль %d + "
            "суммаризация %d + долговременная %d + задача %d + память %d + вопрос %d + схемы %d) "
            "из окна %d · за окном %d сообщ. ≈ %d токенов",
            self.profile.name, self.id, report.estimated, report.breakdown.system,
            report.breakdown.persona, report.breakdown.summary, report.breakdown.long,
            report.breakdown.task, report.breakdown.memory, report.breakdown.question,
            report.breakdown.tools, report.limit, beyond, beyond_tokens,
        )

        raw, first_usage = None, None
        for _ in range(max(1, self.max_steps)):             # 4. цикл работы
            raw = totals.add(self._call(messages, specs))
            first_usage = first_usage or raw.get("usage")   #    факт по первому запросу
            if not raw["tool_calls"]:
                break                                       #    модель дала ответ
            messages.append(raw["message"])                 #    протокол требует вернуть
            for call in raw["tool_calls"]:                  #    запрос вызова в диалог
                step = self._run_tool(len(steps) + 1, call)
                steps.append(step)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": step.result})
                self._notify("step", step.to_dict())
                # Шаг мог создать файл или закрыть пункт плана — а значит, сдвинуть
                # этап. Применяем правила сразу, чтобы автомат шёл вместе с работой.
                self._live_rules([], [step])
        else:
            # Шаги кончились, а модель всё ещё зовёт инструменты — просим итог.
            logger.warning("Агент «%s» [%s]: исчерпан лимит в %d шагов", self.profile.name, self.id, self.max_steps)
            messages.append({"role": "system", "content": FINISH_NUDGE})
            raw = totals.add(self._call(messages, None))

        answer = self._postprocess(raw["content"], steps)   # 5. разбор ответа
        violations = self._check_invariants(answer)         #    сверка ответа с инвариантами
        self._check_persona(answer)                         #    сверка ответа с профилем пользователя
        shadow = None
        if compare:
            shadow = self._shadow(plan, text, specs, totals)
        elif compare_with is not None:
            shadow = self._shadow(plan, text, specs, totals, who=self._other_persona(compare_with))
        self.turns += 1
        pair = self._remember(text, answer)                 # 6. краткосрочный слой: окно и очереди
        routed = self._route(plan, steps, totals)           #    рабочий и долговременный слои
        personal = self._persona_report(report)             #    профиль: правки за обращение и сверка
        folded = self._compress(totals)                     #    сжатие: очередь набралась — свернуть
        self._close_report(report, totals, raw, first_usage)
        self._save(pair, report, totals, shadow, routed)    #    история, состояние, расход — одной записью

        elapsed = time.perf_counter() - started
        logger.info(
            "Агент «%s» [%s] → ответ #%d за %.2f c · шагов=%d · вызовов модели=%d · слои: краткосрочная "
            "%d сообщ., рабочая %s, долговременная %d зап. · ждут суммаризации %d, ждут маршрутизации %d "
            "· токены: оценка %d → факт %d (%s%%), ответ %d, всего за обращение %d",
            self.profile.name, self.id, self.turns, elapsed, len(steps), totals.llm_calls,
            len(self._memory), f"«{self._task.title}»" if self._task else "нет задачи",
            len(self._long.notes), len(self._pending_summary), len(self._pending_route),
            report.estimated, report.prompt_tokens, report.error_pct, report.completion_tokens,
            report.total_prompt + report.total_completion,
        )

        return AgentReply(
            text=answer,
            model=raw["model"],
            turn=self.turns,
            plan=plan,
            steps=steps,
            llm_calls=totals.llm_calls,
            finish_reason=raw["finish_reason"],
            usage=totals.usage(),
            elapsed_s=round(elapsed, 3),
            cost_usd=totals.cost_usd if totals.has_cost else None,
            temperature=raw["temperature"],
            tier=raw["tier"],
            sent_messages=len(messages),
            tokens=report,
            compression=folded,
            memory=routed,
            persona=personal,
            violations=violations,
            shadow=shadow,
            request=raw["request"],
            response=raw["response"],
        )

    # ---------------------------------------------------------- шаги обработки --

    def _prepare(self, message: str) -> str:
        """Шаг 1. Проверить и нормализовать вход — до всякого обращения к API.

        Пустое и слишком длинное отсекаем здесь: вызов модели не должен уходить
        с заведомо непригодным запросом.
        """
        text = (message or "").strip()
        if not text:
            raise AgentError("Пустой запрос: агенту нечего обрабатывать.")
        if len(text) > config.AGENT_MAX_INPUT_CHARS:
            raise AgentError(
                f"Запрос слишком длинный: {len(text)} символов при лимите "
                f"{config.AGENT_MAX_INPUT_CHARS}."
            )
        return text

    def _make_plan(self, text: str, totals: _Totals) -> list[str]:
        """Шаг 2. Отдельным вызовом решить, нужен ли план, и получить его шаги.

        План — это не украшение: он попадает в system-сообщение исполнителя, и
        дальше агент идёт по нему. На простой вопрос планировщик возвращает пустой
        список — тогда лишней работы не будет.
        """
        if not self.planning:
            return []
        listing = (
            "\n".join(f"- {spec['function']['name']}: {spec['function']['description']}"
                      for spec in self._specs())
            if self.tools_enabled else "- инструментов нет, доступен только текстовый ответ"
        )
        try:
            raw = totals.add(self._call(
                [{"role": "system", "content": PLANNER_SYSTEM.format(tools=listing)},
                 {"role": "user", "content": text}],
                None,
            ))
        except AgentError as e:
            logger.warning("Агент «%s» [%s]: планировщик недоступен (%s) — работаем без плана",
                           self.profile.name, self.id, e)
            return []

        data = _extract_json(raw["content"])
        if not isinstance(data, dict):
            logger.warning("Агент «%s» [%s]: план не разобрать, работаем без него", self.profile.name, self.id)
            return []
        plan = [str(s).strip() for s in data.get("steps", []) if str(s).strip()][:6]
        logger.info("Агент «%s» [%s]: план из %d шагов", self.profile.name, self.id, len(plan))
        return plan

    def _build_messages(self, prompt: str, window: list[dict], text: str) -> list[dict]:
        """Собрать сообщения для модели: роль агента + план + память + новый вход.

        Здесь и видно отличие агента от голого вызова API: интерфейс прислал одну
        строку, а в модель уходит контекст, который агент собрал сам. `window` —
        это память, уже подрезанная под окно контекста (см. `_fit_context`); в
        запрос из неё идут только роль и текст — номера строк истории, которые
        агент держит при сообщениях для себя, модели не нужны.
        """
        return (
            [{"role": "system", "content": prompt}]
            + [{"role": m["role"], "content": m["content"]} for m in window]
            + [{"role": "user", "content": text}]
        )

    def _specs(self) -> list[dict] | None:
        """Схемы инструментов для модели: рабочие плюс инструменты памяти.

        Инструменты памяти (`remember`, `recall`) идут вместе с остальными и по
        тому же тумблеру: обещать модели то, чего у неё нет, нельзя, а схемы в
        любом случае платные. Выключены оба слоя — их схемы не уходят тоже.
        """
        if not self.tools_enabled:
            return None
        return (tools.specs() + memory.tool_specs(self._long_on, self.working)
                + persona.tool_specs(self._persona is not None))

    @property
    def _long_on(self) -> bool:
        """Включён ли долговременный слой — это и есть стратегия «Факты»."""
        return self.strategy == "facts"

    def _context_limit(self) -> int:
        """Окно контекста выбранной модели, токенов."""
        return config.model_context(self.model)

    def _answer_reserve(self) -> int:
        """Сколько токенов окна держим под ответ: контекст общий на запрос и ответ.

        Если лимит ответа задан явно — резервируем ровно его, иначе берём запас по
        умолчанию, но не больше того, что модель вообще способна сгенерировать. И
        в любом случае не больше половины окна: у модели с тесным контекстом запас
        под ответ иначе съел бы место под сам запрос.
        """
        wanted = int(self.max_tokens) if self.max_tokens else min(
            config.ANSWER_RESERVE, config.model_max_output(self.model)
        )
        return max(1, min(wanted, self._context_limit() // 2))

    def _fit_context(self, prompt: str, question: str, specs: list[dict] | None) -> tuple[list[dict], int]:
        """Уложить запрос в окно контекста, при нехватке — забыв самое старое.

        Это и есть поведение агента при переполнении. Ждать ошибки от API нельзя:
        она приходит после того, как запрос уже ушёл, и ничего не объясняет. Агент
        считает вес запроса сам и выкидывает из окна самые старые пары «вопрос-
        ответ», пока запрос не поместится, — ценой того, что начало разговора он
        забывает. История на диске при этом цела: подрезается только контекст.

        Если не помещается даже запрос без памяти (огромный вопрос, раздутая
        инструкция), честнее отказаться до вызова: платить за заведомо отклонённый
        запрос незачем.
        """
        room = self._context_limit() - self._answer_reserve()
        fixed = tokens.measure(prompt, [], question, specs, self.model).total
        if fixed > room:
            raise AgentError(
                f"Запрос не помещается в контекст модели: без памяти это уже ≈{fixed} токенов "
                f"при доступных {room} (окно {self._context_limit()} минус запас под ответ "
                f"{self._answer_reserve()}). Сократите вопрос, выключите инструменты или "
                f"возьмите модель с большим окном."
            )
        window = list(self._memory)
        dropped = 0
        while window and fixed + tokens.measure_messages(window, self.model) > room:
            del window[:2]      # самая старая пара «вопрос-ответ» уходит первой
            dropped += 1
        if dropped:
            logger.warning(
                "Агент «%s» [%s]: контекст переполнен — из памяти выброшено %d пар(ы), "
                "в запрос уйдут только последние %d сообщ.",
                self.profile.name, self.id, dropped, len(window),
            )
        return window, dropped

    def _close_report(
        self,
        report: TokenReport,
        totals: _Totals,
        raw: dict,
        first_usage: dict | None,
    ) -> None:
        """Дописать в счёт фактические токены и сверить с ними собственную оценку.

        Сверка нужна не для красоты: счётчик оценивает текст по символам, и без
        сравнения с `usage` невозможно понять, можно ли доверять его прогнозу и
        проверке на переполнение. Расхождение запоминается в калибровке модели —
        следующая оценка будет точнее.
        """
        if first_usage:
            report.prompt_tokens = first_usage["prompt_tokens"]
            tokens.observe(self.model, report.estimated, report.prompt_tokens)
        last_usage = raw.get("usage") or {}
        report.completion_tokens = last_usage.get("completion_tokens", 0)
        totals_usage = totals.usage() or {}
        report.total_prompt = totals_usage.get("prompt_tokens", 0)
        report.total_completion = totals_usage.get("completion_tokens", 0)
        report.truncated = raw.get("finish_reason") == "length"
        if report.truncated:
            logger.warning(
                "Агент «%s» [%s]: ответ оборван по лимиту генерации (%d токенов)",
                self.profile.name, self.id, report.completion_tokens,
            )

    def _system_prompt(
        self,
        plan: list[str],
        window: list[dict] | None = None,
        blocks: bool = True,
        who: object = _SELF,
    ) -> str:
        """Роль агента, дополненная правилами про инструменты, слои памяти, профиль и план.

        Роль пишет пользователь, и полагаться на неё в этих вопросах нельзя: про
        инструменты, суммаризацию, долговременную память, текущую задачу и само
        наличие памяти агент рассказывает модели сам. `memory` — окно, которое
        пойдёт следом (по умолчанию своё), `blocks=False` собирает инструкцию без
        блоков слоёв — так строится теневой запрос «с полной историей».

        `who` — чей профиль подключить: по умолчанию свой, `None` — вообще без
        профиля, другой профиль — для теневого сравнения «ответы для разных
        профилей». Профиль идёт последним блоком, перед самим вопросом: это
        требования к ответу, и модель точнее держит их, когда они рядом с задачей.
        """
        window = self._memory if window is None else window
        prompt = self.profile.instructions
        profile = self._persona if who is _SELF else who
        if self.tools_enabled:
            prompt += TOOLS_NOTE
            prompt += f"\n\nРабочая папка для файловых инструментов: {tools.workspace()}"
            if self._long_on or self.working:
                prompt += MEMORY_TOOLS_NOTE
            if profile is not None:
                prompt += persona.TOOLS_NOTE
        block = (self._summary_block() + self._long_block() + self._invariants_block()
                 + self._task_block()) if blocks else ""
        prompt += block
        if window:
            prompt += MEMORY_NOTE.format(count=len(window), when=self._last_seen())
        elif not block:
            prompt += NO_MEMORY_NOTE
        if plan:
            listed = "\n".join(f"{i}. {step}" for i, step in enumerate(plan, 1))
            prompt += (
                "\n\nТы сам составил план на эту задачу:\n" + listed +
                "\nСледуй ему. Если по ходу дела план оказался неверным — скажи об этом в ответе."
            )
        if profile is not None:
            prompt += profile.block()
        return prompt

    def _summary_block(self) -> str:
        """Суммаризация в том виде, в каком она уходит в инструкцию.

        Пусто, если сворачивать пока нечего или сжатие выключено: выключенное сжатие
        не стирает суммаризацию, а лишь перестаёт её подставлять — включат обратно,
        и она снова в деле.
        """
        if not self.summarize or not self._summary:
            return ""
        return SUMMARY_NOTE.format(count=self._summary.messages, summary=self._summary.text)

    def _long_block(self) -> str:
        """Долговременная память в том виде, в каком она уходит в инструкцию.

        Пусто, если слой выключен (стратегия не «Факты») или в нём пока ничего нет.
        Выключенный слой память не стирает: она остаётся в базе и вернётся в запрос,
        как только стратегию выбрать снова.
        """
        if not self._long_on or not self._long:
            return ""
        return memory.LONG_NOTE.format(count=len(self._long.notes), notes=self._long.text())

    def _task_block(self) -> str:
        """Карточка текущей задачи в том виде, в каком она уходит в инструкцию.

        Пусто, если рабочая память выключена, задачи нет или она уже закрыта: в этом
        и смысл слоя — закончилась задача, и её данные перестают занимать контекст.

        В запрос уходит не вся карточка, а только части, нужные текущему этапу
        (`config.state_sections`), плюс сам этап, номер шага, ожидаемое действие и
        правило этапа. Это и есть «инжектируй только нужное для текущего шага»: на
        планировании агенту незачем видеть созданные файлы, на проверке — наоборот,
        нужны все.

        На паузе вместо карточки уходит закладка в одну строку: дело отложено, и
        платить за него контекстом в каждом сообщении незачем — но знать, что оно
        есть, агент должен, иначе вернуться к нему сам он не предложит.
        """
        if not self.working or self._task is None or not self._task.open or not self._task:
            return ""
        task, state = self._task, self._task.state
        if task.paused:
            return memory.PAUSED_NOTE.format(bookmark=task.bookmark())
        return memory.TASK_NOTE.format(
            state=config.state_label(state),
            en=config.state_en(state),
            what=config.TASK_STATE_BY_CODE[state]["what"],
            step=task.step,
            total=task.total,
            current=task.current_step(),
            expect=task.expect_line() or "ход за вами",
            exit=config.state_exit(state) or "этап последний",
            task=task.text(config.state_sections(state)),
            # Просьба продолжить с места стоит ровно одно обращение — то самое,
            # которое идёт первым после паузы. Дальше продолжение уже не первое.
            resume=memory.RESUME_NOTE if task.resuming else "",
            rule=config.state_rule(state),
        )

    def _invariants_block(self) -> str:
        """Инварианты в том виде, в каком они уходят в инструкцию.

        Отдельно от остальной долговременной памяти: это не воспоминание, а закон,
        и после ответа он ещё и проверяется (`_check_invariants`).
        """
        if not self._long_on:
            return ""
        items = self._long.invariants()
        if not items:
            return ""
        return memory.INVARIANTS_NOTE.format(count=len(items), items=self._long.invariants_text())

    def _persona_block(self) -> str:
        """Профиль пользователя в том виде, в каком он уходит в инструкцию.

        Пусто, если профиль не подключён. В отличие от слоёв памяти, у профиля нет
        «накопления»: он полностью известен заранее, поэтому и в запрос уходит
        всегда целиком — и стоит одинаково в каждом обращении.
        """
        return self._persona.block() if self._persona is not None else ""

    def _call(
        self,
        messages: list[dict],
        specs: list[dict] | None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """Один вызов модели через транспорт; сбой превращаем в ошибку агента.

        Температура и лимит ответа по умолчанию — настройки агента; служебные
        вызовы (суммаризация, факты) передают свои.
        """
        try:
            return llm.chat(
                messages,
                model=self.model,
                temperature=self.temperature if temperature is None else temperature,
                max_tokens=self.max_tokens if max_tokens is None else max_tokens,
                tools=specs,
            )
        except Exception as e:  # 401/403/429, сеть и прочее
            logger.warning("Агент «%s» [%s]: вызов модели не удался: %s", self.profile.name, self.id, e)
            text = str(e)
            if any(marker in text.lower() for marker in LENGTH_ERROR_MARKERS):
                # Переполнение, которое проскочило собственную проверку: оценка по
                # символам не токенайзер и может ошибиться. Показываем не «400», а
                # то, из-за чего запрос отклонён.
                weight = tokens.measure_request(messages, specs, self.model)
                raise AgentError(
                    f"Модель отклонила запрос по длине: в него ушло ≈{weight} токенов при окне "
                    f"{self._context_limit()} и лимите ответа {self._answer_reserve()}. "
                    f"Уменьшите глубину памяти, сократите вопрос или начните разговор заново.\n"
                    f"Ответ провайдера: {text}"
                ) from e
            raise AgentError(f"Модель не ответила: {e}") from e

    def _run_tool(self, number: int, call: dict) -> AgentStep:
        """Шаг 3. Выполнить то, что попросила модель, и записать результат в трассу.

        Ошибку инструмента не поднимаем наверх: она возвращается модели как
        результат, и агент получает шанс исправиться на следующем шаге.

        Инструменты памяти (`remember`, `recall`) исполняет сам агент: обычные
        инструменты — чистые функции и про агента ничего не знают, а эти пишут в
        его собственные слои. Схемы у них при этом общие с остальными.
        """
        name = call["name"]
        try:
            arguments = json.loads(call["arguments"] or "{}")
        except (json.JSONDecodeError, TypeError):
            arguments = {"_raw": call["arguments"]}

        started = time.perf_counter()
        if name in memory.TOOL_NAMES:
            result, ok = self._memory_tool(name, arguments)
        elif name in persona.TOOL_NAMES:
            result, ok = self._persona_tool(name, arguments)
        else:
            try:
                result, ok = tools.call(name, call["arguments"]), True
            except tools.ToolError as e:
                result, ok = f"Ошибка инструмента: {e}", False
        elapsed = round(time.perf_counter() - started, 3)

        logger.info(
            "Агент «%s» [%s] · шаг %d: %s(%s) → %s за %.2f c",
            self.profile.name, self.id, number, name,
            ", ".join(f"{k}={_short(v)}" for k, v in arguments.items()),
            "ок" if ok else "ошибка", elapsed,
        )
        tool = tools.BY_NAME.get(name)
        title = tool.title if tool else (
            memory.TOOL_TITLES.get(name) or persona.TOOL_TITLES.get(name, name))
        return AgentStep(
            number=number,
            tool=name,
            title=title,
            arguments=arguments,
            result=result,
            ok=ok,
            elapsed_s=elapsed,
        )

    def _check_invariants(self, answer: str) -> list[str]:
        """Сверить свой же ответ с инвариантами долговременной памяти.

        Правило, написанное в инструкции словами, — просьба: модель может её
        нарушить, и без проверки этого никто не заметит. Здесь нарушение
        становится фактом, который видно под ответом. Ответ при этом не
        перегенерируется: агент показывает нарушение, а решение — за человеком.
        """
        if not self._long_on:
            return []
        violations = memory.check_invariants(answer, self._long)
        if violations:
            logger.warning("Агент «%s» [%s]: ответ нарушает инварианты — %s",
                           self.profile.name, self.id, "; ".join(violations))
        return violations

    def _memory_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Исполнить инструмент памяти: агент кладёт в свой слой или читает из него.

        Это третий способ выбрать, что куда сохранить (первые два — правила в коде и
        маршрутизатор), и единственный, где выбор делает сам агент по ходу работы.
        Запись сразу попадает в трассу обращения, поэтому на экране видно не только
        «что запомнено», но и «кто решил запомнить».
        """
        try:
            if name == "recall":
                return memory.apply_recall(arguments, self._long, self._task), True
            if self.working and (self._task is None or not self._task.open):
                self._ensure_task(str(arguments.get("value") or ""), turn=self.turns + 1)
            result, route = memory.apply_remember(
                arguments, self._long, self._task, self.turns + 1, self._long_on, self.working
            )
            self._tool_routes.append(route)
            logger.info("Агент «%s» [%s]: инструмент памяти — %s → %s",
                        self.profile.name, self.id, route.layer, route.what)
            return result, True
        except ValueError as e:
            return f"Ошибка инструмента: {e}", False

    def _persona_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Исполнить `prefer`: агент сам правит профиль своего собеседника.

        Третий источник правок профиля — тот, где решение принимает агент по ходу
        разговора («просили короче — закреплю в профиле»). Как и у инструментов
        памяти, схема живёт в своём модуле, а исполнение здесь: правится профиль
        этого агента, и знать о нём чистая функция инструмента не может.
        """
        try:
            result, change = persona.apply_prefer(arguments, self._persona, self.turns + 1)
        except ValueError as e:
            return f"Ошибка инструмента: {e}", False
        if change.action != "keep":
            self._persona_update.changes.append(change)
            logger.info("Агент «%s» [%s]: инструмент профиля — %s",
                        self.profile.name, self.id, change.what)
        return result, True

    def _check_persona(self, answer: str) -> None:
        """Сверить готовый ответ с профилем: длина, формат и ограничения.

        Ровно то, чего не хватает обычной персонализации «через промпт»: просьба
        уходит в модель, а соблюли её или нет — никто не смотрит. Ответ при
        нарушении не перегенерируется (это было бы вдвое дороже), но расхождение
        показывается под ответом.
        """
        self._persona_update.checks = persona.check(answer, self._persona)
        broken = self._persona_update.broken
        if broken:
            logger.warning(
                "Агент «%s» [%s]: ответ разошёлся с профилем «%s» — %s",
                self.profile.name, self.id, self._persona.name if self._persona else "",
                "; ".join(f"{check.label}: {check.detail}" for check in broken),
            )

    def _persona_report(self, report: TokenReport) -> "persona.PersonaUpdate | None":
        """Собрать итог по профилю за обращение: правки, отказы и сверка ответа."""
        update = self._persona_update
        update.tokens = report.breakdown.persona
        if self._persona is not None:
            update.name = self._persona.name
        if not update:
            return None
        if update.changes:
            logger.info(
                "Агент «%s» [%s]: профиль «%s» обновлён — %s",
                self.profile.name, self.id, update.name,
                "; ".join(f"{change.what} ({config.persona_source_label(change.source)})"
                          for change in update.changes),
            )
        return update

    def _live_rules(self, plan: list[str], steps: list[AgentStep]) -> None:
        """Применить правила рабочей памяти прямо по ходу обращения.

        Раньше это делалось один раз, после ответа, и получалось так: агент за одно
        обращение составлял план, выполнял его и доходил до проверки — а в окне это
        появлялось разом, уже свершившимся фактом. Теперь план и каждый выполненный
        шаг ложатся в карточку сразу, и автомат идёт вместе с работой.
        """
        if not self.working or (not plan and not steps and self._task is None):
            return
        if self._task is not None and self._task.paused:
            return          # отложенная задача не обрастает работой, которая уже не про неё
        # Заводить карточку по плану стоит, только если план и правда про дело:
        # планировщик выдаёт один шаг и на «напомни, что ты знаешь», и от этого
        # заводилась пустая задача-призрак (поймано на живом прогоне). Один шаг —
        # ждём маршрутизатора, он разберётся лучше.
        if self._task is not None or len(plan) > 1:
            task = self._ensure_task(turn=self.turns + 1)
            self._live.routes.extend(memory.rules_from_turn(task, plan, steps))
            self._apply_transition(task, memory.overdue_state(task), self._live, source="rule")

    def _ensure_task(self, hint: str = "", turn: int = 0) -> "memory.Task":
        """Взять открытую задачу или завести новую — рабочей памяти нужно куда писать.

        `turn` — номер обращения, на котором задача заводится. Он приходит снаружи,
        потому что счётчик обращений растёт в середине `ask()`: инструмент зовётся
        до, маршрутизатор — после, а в карточке должно стоять одно и то же число.
        """
        if self._task is None or not self._task.open:
            self._task = memory.Task(title=" ".join(hint.split())[:60], turn=turn or self.turns,
                                     at=time.time(), updated_at=time.time())
            memory.refresh_expect(self._task)   # ожидаемое действие есть у задачи с первой секунды
        return self._task

    def _postprocess(self, content: str, steps: list[AgentStep]) -> str:
        """Шаг 4. Привести ответ модели к тому, что агент готов отдать наружу.

        Если итога нет, но шаги выполнены (упёрлись в лимит, модель не подвела
        черту), обращение не роняем: работа уже сделана, и честнее показать, что
        именно успел агент, чем отдать ошибку.
        """
        answer = (content or "").strip()
        if answer:
            return answer
        if steps:
            done = ", ".join(f"{s.tool}{'' if s.ok else ' (ошибка)'}" for s in steps)
            return ("Итог не сформулирован — модель остановилась без финального ответа. "
                    f"Выполненные шаги: {done}.")
        raise AgentError(
            "Модель вернула пустой ответ — возможно, генерация упёрлась в ограничение длины."
        )

    def _remember(self, question: str, answer: str) -> list[dict]:
        """Шаг 6. Положить обмен в краткосрочный слой — это первое правило маршрутизации.

        В окне держим только вопрос и итоговый ответ: служебная переписка с
        инструментами нужна внутри одного обращения, а в памяти она бы быстро съела
        контекст и деньги. Та же пара сразу встаёт в очередь к маршрутизатору — он
        решит, есть ли в ней что-то для рабочего и долговременного слоёв. Что при
        этом выпало из окна, уходит в очередь на суммаризацию (см. `_trim_memory`) —
        если сжатие включено.
        """
        pair = [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ]
        self._memory.extend(pair)
        if self._routing:
            self._pending_route.extend(pair)   # маршрутизатор смотрит каждый обмен, а не выпавшее
        self._trim_memory()
        return pair

    @property
    def _routing(self) -> bool:
        """Есть ли кому маршрутизировать: управляемый слой памяти или профиль.

        Профиль попал сюда не для симметрии: просьбу «отвечай короче» замечает тот
        же вызов, и без него персонализация осталась бы только ручной.
        """
        return self._long_on or self.working or self._persona is not None

    def _save(
        self,
        pair: list[dict],
        report: TokenReport,
        totals: _Totals,
        shadow: Shadow | None = None,
        routed: memory.MemoryUpdate | None = None,
    ) -> None:
        """Записать обращение в хранилище: сообщения, состояние, расход и оба слоя памяти.

        Одной записью, чтобы история, счётчик обращений и токены не разъезжались.
        Строка расхода и превращает «сколько стоило» в наблюдаемую величину: по
        ней видно, как цена растёт от обращения к обращению — и как перестаёт
        расти, когда в дело вступает стратегия. Долговременная память и карточка
        задачи пишутся той же транзакцией: граница долговременного слоя — последнее
        сообщение этой пары, иначе записи свежего обмена ждали бы ход.
        """
        if self.store is None:
            return
        notes_row = task_row = None
        if routed is not None:
            notes_row = {"version": self._long.version, "upto": self._long.upto,
                         "rows": self._long.items()}
            if self._task is not None:
                task_row = self._task.row()
        saved = self.store.save_turn(
            self.state(), pair, self._usage_row(report, totals, shadow),
            notes=notes_row, task=task_row,
        )
        for message, number in zip(pair, saved["messages"]):
            message["id"] = number   # окно помнит, какой строке истории отвечает сообщение
        if notes_row is not None and saved["messages"]:
            self._long.upto = saved["messages"][-1]
        if task_row is not None and saved["task"]:
            self._task.id = saved["task"]
        # Профиль пишется отдельно, а не той же транзакцией: он не принадлежит ни
        # этому разговору, ни этой ветке — им пользуются и другие агенты, и он
        # переживёт даже «забыть разговор».
        if self._persona is not None and self._persona_update.changes:
            self.store.save_persona(self._persona.row())

    def _usage_row(self, report: TokenReport, totals: _Totals, shadow: Shadow | None = None) -> dict:
        """Расход обращения одной строкой — то, из чего потом рисуется рост цены."""
        return {
            "turn": self.turns,
            "model": self.model,
            "prompt_tokens": report.total_prompt,
            "completion_tokens": report.total_completion,
            "total_tokens": report.total_prompt + report.total_completion,
            "cost_usd": totals.cost_usd if totals.has_cost else None,
            "llm_calls": totals.llm_calls,
            "estimated": report.estimated,
            "context_tokens": report.context_used,
            "memory_tokens": report.breakdown.memory,
            "context_limit": report.limit,
            "trimmed_pairs": report.trimmed_pairs,
            "summary_tokens": report.breakdown.summary,
            "folded_messages": report.folded_messages,
            "folded_tokens": report.folded_tokens,
            "shadow_tokens": shadow.prompt_tokens + shadow.completion_tokens if shadow else 0,
            "strategy": self.strategy,
            "summarize": int(self.summarize),
            "branch": self.branch,
            "dropped_messages": report.dropped_messages,
            "dropped_tokens": report.dropped_tokens,
            "long_tokens": report.breakdown.long,
            "task_tokens": report.breakdown.task,
            "long_items": report.long_items,
            "task_items": report.task_items,
            "persona_tokens": report.breakdown.persona,
            "persona": report.persona_id,
        }

    def _trim_memory(self) -> None:
        """Оставить в окне последние `memory_turns` пар; выпавшее — по правилам сжатия.

        Без сжатия выпавшие сообщения просто перестают уходить в модель (в истории
        они остаются; фактам они и не нужны — блок уже собран из них). Со сжатием
        они встают в очередь и ждут, пока наберётся на обновление суммаризации.
        """
        extra = len(self._memory) - max(0, self.memory_turns) * 2
        if extra > 0:
            overflow = self._memory[:extra]
            del self._memory[:extra]
            if self.summarize:
                self._pending_summary.extend(overflow)

    def _compress(self, totals: _Totals) -> Compression | None:
        """Свернуть очередь в суммаризацию, если она набралась.

        Это и есть сжатие истории: сообщения, выпавшие из окна, не выбрасываются и
        не уходят в модель дословно, а превращаются отдельным вызовом в короткий
        список фактов. Вызов устроен как у планировщика — своя роль, низкая
        температура, строгий формат. Прежняя суммаризация подаётся на вход, поэтому
        новая — суммаризация всего разговора, а не только последних сообщений.

        Сжатие — опция, а не стратегия: оно включается поверх любой из них, поэтому
        сюда агент заходит и со скользящим окном, и с фактами, и в ветке.

        Сбой здесь не роняет обращение: ответ пользователь уже получил, а очередь
        подождёт следующего раза.
        """
        if not self.summarize:
            return None
        # С хранилищем сворачиваем только то, что уже записано в историю (у таких
        # сообщений есть номер строки): по нему после перезапуска видно, что уже в
        # суммаризации, а что ещё нет. Без хранилища сворачиваем всё, что накопилось.
        batch = [m for m in self._pending_summary if self.store is None or m.get("id")]
        if len(batch) < max(1, self.summary_every):
            return None

        listing = "\n".join(
            f"[{'пользователь' if m['role'] == 'user' else 'агент'}] {m['content']}" for m in batch
        )
        started = time.perf_counter()
        try:
            raw = totals.add(self._call(
                [{"role": "system", "content": SUMMARY_SYSTEM.format(
                    name=self.profile.name, points=config.SUMMARY_POINTS)},
                 {"role": "user", "content": SUMMARY_USER.format(
                     summary=self._summary.text or "(пока пуста)", count=len(batch), messages=listing)}],
                None,
                temperature=config.SUMMARY_TEMPERATURE,
                max_tokens=config.SUMMARY_MAX_TOKENS,
            ))
        except AgentError as e:
            logger.warning("Агент «%s» [%s]: суммаризация не обновлена (%s) — очередь подождёт",
                           self.profile.name, self.id, e)
            return None
        text = (raw["content"] or "").strip()
        if not text:
            logger.warning("Агент «%s» [%s]: модель вернула пустую суммаризацию — оставляем прежнюю",
                           self.profile.name, self.id)
            return None

        before = tokens.measure_messages(batch, self.model)
        after = tokens.measure_text(text, self.model)
        self._summary = Summary(
            text=text,
            version=self._summary.version + 1,
            upto=max([self._summary.upto] + [m.get("id") or 0 for m in batch]),
            messages=self._summary.messages + len(batch),
            tokens=self._summary.tokens + before,
            at=time.time(),
        )
        taken = {id(m) for m in batch}
        self._pending_summary = [m for m in self._pending_summary if id(m) not in taken]
        if self.store is not None:
            self.store.save_summary(self.id, {
                "version": self._summary.version,
                "turn": self.turns,
                "upto": self._summary.upto,
                "folded_messages": self._summary.messages,
                "folded_tokens": self._summary.tokens,
                "summary_tokens": after,
                "content": text,
            }, self.branch)
        usage = raw.get("usage") or {}
        logger.info(
            "Агент «%s» [%s]: %d сообщ. ≈ %d токенов свёрнуты в суммаризацию №%d ≈ %d токенов "
            "(всего заменяет %d сообщ. ≈ %d токенов)",
            self.profile.name, self.id, len(batch), before, self._summary.version, after,
            self._summary.messages, self._summary.tokens,
        )
        return Compression(
            messages=len(batch),
            before=before,
            after=after,
            version=self._summary.version,
            text=text,
            elapsed_s=round(time.perf_counter() - started, 3),
            call_tokens=usage.get("total_tokens", 0),
        )

    def _route(self, plan: list[str], steps: list[AgentStep], totals: _Totals) -> memory.MemoryUpdate | None:
        """Разложить всё новое по слоям памяти — сердце модели памяти.

        Здесь сходятся все три способа выбрать, что куда сохранить:

            правила      детерминированный код (`memory.rules_from_turn`): план стал
                         шагами задачи, выполненный инструмент — находкой, записанный
                         файл — артефактом. Никакой модели, никаких токенов;
            инструмент   то, что агент положил сам по ходу цикла (`_tool_routes`);
            маршрутизатор отдельный вызов модели: он получает карточку задачи,
                         долговременную память и новые сообщения, а возвращает оба
                         слоя целиком — изменившаяся запись заменяется, отменённая
                         исчезает.

        Закрытая маршрутизатором задача уходит из запроса, но её итог переезжает в
        долговременную память: рабочий слой эфемерен, а «мы это сделали» — нет.

        Сбой вызова не роняет обращение: ответ пользователь уже получил, а правила и
        записи инструмента в слоях остаются — очередь подождёт следующего раза.
        """
        if not self._routing:
            return None

        started = time.perf_counter()
        task_before = self._task.to_dict() if self._task else None

        # Правила уже отработали по ходу обращения (`_live_rules`): план стал шагами,
        # выполненные инструменты — находками и артефактами, этап успел сдвинуться.
        # Здесь остаётся добрать то, чего цикл не видел, и добавить записи инструмента.
        update = self._live
        update.routes[:0] = self._tool_routes
        self._live_rules(plan, steps)
        batch = self._pending_route[:config.ROUTER_BATCH]
        if batch:
            called = self._call_router(batch, totals, update)
            if called:
                del self._pending_route[:len(batch)]
                update.messages = len(batch)

        if self._task is not None and self._task.open:
            # Автомат не должен отставать от факта: если работа уже идёт, а этап
            # всё ещё «Планирование», код двигает его сам — без модели и без
            # человека. Иначе задача висит на первом этапе с выполненными шагами
            # (поймано на живом прогоне и на скриншоте пользователя).
            self._apply_transition(self._task, memory.overdue_state(self._task), update,
                                   source="rule")
            # Ожидаемое действие пересчитываем последним: этап и шаги к этому моменту
            # окончательные. Уточнение маршрутизатора при этом не трогаем — оно
            # конкретнее шаблона и относится к этому же обмену.
            if not any(r.kind == "expect" and r.source == "router" for r in update.routes):
                route = memory.refresh_expect(self._task)
                if route is not None:
                    update.routes.append(route)
            if self._resumed_before and self._task.resuming:
                # Просьба «продолжай с места» отработала в этом обращении. Не снять
                # её здесь — и она висела бы в каждом следующем запросе, хотя
                # продолжение давно не первое.
                self._task.resuming = False

        if self._task is not None and not self._task.open and task_before and task_before["open"]:
            handed = memory.handoff(self._task, self._long, self.turns)
            if handed is not None:
                update.routes.append(handed)

        # Карточка, заведённая в этом обращении и оставшаяся без названия и цели, —
        # не задача, а призрак: маршрутизатор её не подтвердил. Выбрасываем, иначе в
        # архиве копятся пустые строки.
        if (task_before is None and self._task is not None
                and not self._task.title and not self._task.goal):
            self._task = None
            update.routes = [r for r in update.routes if r.layer != "working"]
            update.moved.clear()

        update.task_action = _task_action(task_before, self._task)
        if update.routes or update.task_action:
            self._long.version += 1
        update.version = self._long.version
        update.long_items = len(self._long.notes)
        update.long_tokens = tokens.measure_text(
            self._long_block() + self._invariants_block(), self.model)
        update.task_tokens = tokens.measure_text(self._task_block(), self.model)
        update.task = self._task.to_dict() if self._task else None
        update.elapsed_s = round(time.perf_counter() - started, 3)
        if not update:
            return None
        logger.info(
            "Агент «%s» [%s]: маршрутизация №%d — %d запис(и) по слоям: %s · долговременная %d зап. "
            "≈ %d токенов · задача %s ≈ %d токенов",
            self.profile.name, self.id, update.version, len(update.routes),
            "; ".join(f"{config.layer_label(r.layer)} {r.action} {r.what}" for r in update.routes[:6])
            or "без изменений",
            update.long_items, update.long_tokens,
            f"«{self._task.title}» ({update.task_action or 'без изменений'})" if self._task else "нет",
            update.task_tokens,
        )
        return update

    def _call_router(self, batch: list[dict], totals: _Totals, update: memory.MemoryUpdate) -> bool:
        """Вызов маршрутизатора: разложить новые сообщения по слоям памяти и профилю.

        Профиль ездит тем же вызовом, а не своим: второй вызов после каждого ответа
        стоил бы столько же, сколько первый, а решает ту же задачу — «что нового
        прозвучало и куда это положить». Свои правила и свою часть JSON-схемы
        профиль приносит сам (`app/persona.py`), поэтому модель памяти про него
        по-прежнему не знает.
        """
        personal = self._persona is not None
        try:
            raw = totals.add(self._call(
                memory.router_messages(
                    self.profile.name, self._task, self._long, batch,
                    extra_rules=persona.router_rules() if personal else "",
                    extra_schema=persona.ROUTER_SCHEMA if personal else "",
                    extra_input=persona.router_input(self._persona),
                ),
                None,
                temperature=config.ROUTER_TEMPERATURE,
                max_tokens=config.ROUTER_MAX_TOKENS,
            ))
        except AgentError as e:
            logger.warning("Агент «%s» [%s]: маршрутизатор недоступен (%s) — очередь подождёт",
                           self.profile.name, self.id, e)
            return False
        parsed = memory.parse_router(raw["content"])
        if parsed is None:
            # Не разобрать — это сбой формата, а не «стереть всю память»: слои остаются
            # как были, очередь дождётся следующего обращения.
            logger.warning("Агент «%s» [%s]: ответ маршрутизатора не разобрать — слои не тронуты",
                           self.profile.name, self.id)
            return False

        update.called = True
        update.call_tokens = (raw.get("usage") or {}).get("total_tokens", 0)
        if personal:
            # Профиль приходит ЧАСТЯМИ, а не целиком, как слои: маршрутизатор
            # возвращает только то, что просит изменить. Иначе он переписывал бы
            # профиль на каждом обращении и затирал выставленное человеком.
            wanted = persona.parse_update(parsed.get("raw"))
            if wanted:
                # То, что агент уже записал инструментом на этом обращении,
                # маршрутизатор не перебивает: он его записи не видел и переписал бы
                # их своими словами, подменив заодно источник (поймано живым прогоном).
                changes, rejected = persona.merge(
                    self._persona, wanted, self.turns, source="router",
                    locked=persona.locked_by(self._persona_update.changes),
                )
                self._persona_update.changes.extend(changes)
                self._persona_update.rejected.extend(rejected)
        if self._long_on and parsed["long"]:
            update.routes.extend(memory.merge_long(self._long, parsed["long"], self.turns))
        if self.working:
            card = parsed["task"]
            if card is None:
                # Маршрутизатор говорит, что задачи нет. Открытую карточку при этом не
                # трогаем: пропавшая из ответа задача чаще означает «разговор ушёл в
                # сторону», чем «дело закончено», а завершает задачу только переход
                # на этап «Готово».
                return True
            task = self._ensure_task(card.get("title", ""), turn=self.turns)
            # Возврат к отложенному делу разбираем ПЕРВЫМ: пока стоит пауза, карточка
            # заморожена, и всё, что маршрутизатор про неё насчитал, применять нельзя.
            # Сняли паузу — дальше обращение идёт как обычное.
            if card.get("paused") is False:
                self._apply_pause(task, False, update)
            if task.paused:
                # Задача так и осталась отложенной: разговор идёт не про неё. Ни
                # шагов, ни находок, ни переходов — иначе «пауза» была бы только
                # словом в интерфейсе.
                return True
            update.routes.extend(memory.merge_task(task, card, self.turns))
            # Сначала догоняем факт, и только потом слушаем совет. Иначе выходит так:
            # пользователь просит завершить, маршрутизатор возвращает done, а задача
            # ещё числится на «Выполнении» — отказ; и лишь следом правило уводит её на
            # «Проверку», откуда завершение было бы разрешено. Один шаг опоздания —
            # и просьба пользователя теряется (поймано на живом прогоне).
            self._apply_transition(task, memory.overdue_state(task), update, source="rule")
            wanted = card.get("state", "")
            if wanted == self._state_before and task.state != self._state_before:
                # Маршрутизатор работал с состоянием на начало обращения и вернул
                # его же — это «двигать рано», а не «вернись назад». Код к тому
                # моменту уже увёл этап вперёд по факту работы, и откат по
                # устаревшему совету гонял бы задачу туда-сюда между этапами.
                wanted = ""
            self._apply_transition(task, wanted, update)
            # Отложить просят последним: всё, что прозвучало в этом обмене, уже
            # разложено по карточке — пауза замораживает её вместе с этим.
            if card.get("paused") is True:
                self._apply_pause(task, True, update)
        return True

    def _apply_transition(
        self,
        task: "memory.Task",
        wanted: str,
        update: memory.MemoryUpdate,
        source: str = "router",
    ) -> None:
        """Применить этап, который попросили, — или отказать.

        Здесь и проходит граница между советом и решением. Модель услужлива по
        природе: попроси её «пропустить планирование» — согласится и вернёт
        `state: done`. Но этап меняет `memory.transition`, сверяясь с таблицей
        разрешённых переходов, и запрещённый переход не применяется, как бы
        убедительно его ни просили. Отказ не прячем: он уходит в трассу и виден
        пользователю — иначе о нём никто не узнает.

        `source` — кто предложил переход: `router` (модель после ответа) или `rule`
        (код, когда этап отстал от факта работы). Применяет его в обоих случаях
        `memory.transition`, и это важно: другого пути сменить этап нет.
        """
        if not wanted:
            return
        if source == "rule" and self._rule_moves:
            # Не больше одного перехода по правилу за обращение. Иначе выходит так:
            # агент в первом же ответе составил план и записал файл, маршрутизатор
            # сложил сделанное в шаги уже закрытыми — и задача, которая только
            # началась, за один обмен уехала с планирования на проверку (поймано на
            # скриншоте пользователя). Наблюдаемые признаки честны только по одному
            # за раз: следующий шаг автомат сделает на следующем обращении.
            logger.info("Агент «%s» [%s]: второй переход по правилу за обращение отложен (%s → %s)",
                        self.profile.name, self.id, task.state, wanted)
            return
        try:
            moved = memory.transition(task, wanted, self.turns)
        except memory.TransitionError as e:
            update.rejected = str(e)
            update.routes.append(memory.Route(
                layer="working", action="reject", kind="state", source=source,
                what=f"переход на «{config.state_label(wanted)}» отклонён",
            ))
            logger.warning("Агент «%s» [%s]: %s", self.profile.name, self.id, e)
            return
        if not moved:
            return
        update.moved.append(moved)
        if source == "rule":
            self._rule_moves += 1
        update.routes.append(memory.Route(
            layer="working", action="change", kind="state", source=source, what=f"этап: {moved}",
        ))
        # Смена этапа — событие, которое стоит показать сразу: пока идёт обращение,
        # по нему видно, чем агент занят прямо сейчас.
        self._notify("state", {"moved": moved, "source": source, "task": task.to_dict()})
        logger.info("Агент «%s» [%s]: этап задачи «%s» — %s (%s)",
                    self.profile.name, self.id, task.title, moved,
                    config.note_source_label(source))

    def _apply_pause(
        self,
        task: "memory.Task",
        wanted: bool | None,
        update: memory.MemoryUpdate,
        source: str = "router",
    ) -> None:
        """Отложить задачу или вернуться к ней по решению маршрутизатора.

        `None` — «поле не упомянуто»: состояние паузы не меняется. Это не мелочь
        разбора, а защита: обычное булево с умолчанием False снимало бы паузу на
        каждом обращении, где модель про поле просто забыла, — а забывает она часто.

        Пауза идёт тем же путём, что и переходы: через `memory.pause`/`resume`, с
        записью в трассу и событием прогресса. Другого способа её поставить нет, и
        поэтому по трассе всегда видно, кто отложил дело — человек или модель.
        """
        if wanted is None or bool(wanted) == task.paused:
            return
        try:
            moved = (memory.pause(task, self.turns) if wanted
                     else memory.resume(task, self.turns))
        except memory.TransitionError as e:
            update.rejected = str(e)
            update.routes.append(memory.Route(
                layer="working", action="reject", kind="pause", source=source,
                what=("пауза отклонена" if wanted else "возврат к задаче отклонён"),
            ))
            return
        if not moved:
            return
        update.paused = moved
        update.routes.append(memory.Route(
            layer="working", action="change", kind="pause", source=source, what=moved,
        ))
        self._notify("pause", {"moved": moved, "source": source, "task": task.to_dict()})
        logger.info("Агент «%s» [%s]: задача «%s» — %s (%s)",
                    self.profile.name, self.id, task.title, moved,
                    config.note_source_label(source))

    def _shadow(
        self,
        plan: list[str],
        text: str,
        specs: list[dict] | None,
        totals: _Totals,
        who: object = _SELF,
    ) -> Shadow:
        """Теневой ответ для сравнения: тот же вопрос, но при других условиях.

        Две оси сравнения, и обе нужны на экране:

            контекст (`who` не задан) — вместо блоков слоёв в запрос уходит вся
                история активной ветки, сколько влезает в окно модели;
            профиль (`who` задан) — контекст ровно тот же, что у настоящего
                ответа, а профиль пользователя другой (или его нет вовсе). Это и
                есть «ответы для разных профилей»: разница в ответах не может
                объясняться ничем, кроме профиля.

        Ответ не запоминается и на разговор не влияет: это измерение, а не
        обращение. Но стоит он по-настоящему, поэтому считается в расход.
        """
        swap = who is not _SELF
        if swap:
            window = list(self._memory)          # контекст тот же, меняется только профиль
            prompt = self._system_prompt(plan, window=window, who=who)
            dropped = 0
        else:
            history = self._full_history()
            prompt = self._system_prompt(plan, window=history, blocks=False)
            room = self._context_limit() - self._answer_reserve()
            fixed = tokens.measure(prompt, [], text, specs, self.model).total
            window, dropped = list(history), 0
            while window and fixed + tokens.measure_messages(window, self.model) > room:
                del window[:2]
                dropped += 1
        breakdown = tokens.measure(prompt, window, text, specs, self.model,
                                   persona=who.block() if swap and who is not None else "")

        started = time.perf_counter()
        raw = totals.add(self._call(self._build_messages(prompt, window, text), specs))
        usage = raw.get("usage") or {}
        content = (raw["content"] or "").strip()
        if raw["tool_calls"] and not content:
            content = (
                "Модель попросила инструменты (" + ", ".join(c["name"] for c in raw["tool_calls"]) +
                "): теневой прогон их не исполняет, сравнивать здесь можно только контекст."
            )
        label = (f"профиль «{who.name}»" if swap and who is not None else
                 "без профиля" if swap else "полная история")
        logger.info(
            "Агент «%s» [%s]: теневой ответ — %s, %d сообщ., %d→%d токенов",
            self.profile.name, self.id, label, len(window), usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )
        return Shadow(
            text=content,
            messages=len(window),
            breakdown=breakdown,
            prompt_tokens=usage.get("prompt_tokens", breakdown.total),
            completion_tokens=usage.get("completion_tokens", 0),
            cost_usd=raw.get("cost_usd"),
            elapsed_s=round(time.perf_counter() - started, 3),
            trimmed_pairs=dropped,
            tool_calls=len(raw["tool_calls"]),
            finish_reason=raw["finish_reason"],
            kind="persona" if swap else "history",
            label=label,
            checks=persona.check(content, who) if swap else [],
        )

    def _other_persona(self, wanted: "str | persona.Persona") -> "persona.Persona | None":
        """Профиль для сравнения: по id из хранилища, объектом или «без профиля».

        Пустая строка — не ошибка, а осмысленный выбор: сравнить ответ с профилем и
        без него. Незнакомый id — ошибка агента, а не молчаливая подмена: иначе
        сравнение показало бы два одинаковых ответа и ничего не объяснило.
        """
        if isinstance(wanted, persona.Persona):
            return wanted
        code = str(wanted or "").strip()
        if not code:
            return None
        if self.store is None:
            raise AgentError("Сравнить профили можно только с хранилищем: профили лежат в нём.")
        row = self.store.persona(code)
        if row is None:
            raise AgentError(f"Профиль «{code}» не найден.")
        return persona.Persona.from_row(row)

    def _full_history(self) -> list[dict]:
        """Вся переписка активной ветки как есть — то, что ушло бы в модель без стратегии."""
        if self.store is not None:
            return [_slim(m) for m in self.store.messages(self.id, self.branch)]
        # Без хранилища свёрнутое уже не вернуть: есть только очередь и окно.
        return [_slim(m) for m in self._pending_summary + self._memory]

    def _beyond_window(self, history: list[dict] | None = None) -> tuple[int, int]:
        """Сколько сообщений истории в модель дословно не уходит — и сколько они весили бы.

        Это то, с чем работают стратегия и сжатие: скользящее окно эти сообщения
        отбрасывает, факты заменяют своим блоком, сжатие ставит их в очередь на
        суммаризацию. Уже свёрнутое не считается — оно в запросе представлено
        суммаризацией.
        """
        if self.store is None:
            rest = self._pending_summary if self.summarize else []
        else:
            history = self._full_history() if history is None else history
            in_window = {m["id"] for m in self._memory if m.get("id")}
            upto = self._summary.upto if self._summary_block() else 0
            rest = [m for m in history if (m.get("id") or 0) > upto and m.get("id") not in in_window]
        return len(rest), tokens.measure_messages(rest, self.model)

    def _load_memory(self) -> None:
        """Собрать окно контекста и обе очереди из истории активной ветки.

        Вызывается при восстановлении агента, при смене глубины памяти, стратегии,
        сжатия, рабочей памяти или ветки: увеличили глубину — агент дотягивает из
        истории то, что уже забыл; включили сжатие — всё, что за окном и ещё не в
        суммаризации, встаёт в очередь на неё; включили слой — в очередь к
        маршрутизатору встаёт всё, что он ещё не разбирал. Свёрнутое в суммаризацию
        в окно не возвращается: платить за это дважды незачем.

        Очередь маршрутизатора ограничена одной порцией: даже если граница слоя
        потерялась (например, память пуста и брать `upto` неоткуда), агент разберёт
        последние `ROUTER_BATCH` сообщений одним вызовом, а не всю историю сразу.
        """
        if self.store is None:
            if not self.summarize:
                self._pending_summary.clear()
            if not self._routing:
                self._pending_route.clear()
            self._trim_memory()
            return
        history = [_slim(m) for m in self.store.messages(self.id, self.branch)]
        if self.summarize:
            history = [m for m in history if m["id"] > self._summary.upto]
        keep = max(0, self.memory_turns) * 2
        window = history[-keep:] if keep else []
        rest = history[:len(history) - len(window)]
        self._memory = window
        self._pending_summary = rest if self.summarize else []
        self._pending_route = (
            [m for m in history if m["id"] > self._long.upto][-config.ROUTER_BATCH:]
            if self._routing else []
        )

    def _load_long(self) -> None:
        """Поднять долговременную память ветки из хранилища.

        Если строк ещё нет, а в базе прошлой версии лежит блок фактов дня 10 — он и
        становится долговременной памятью: терять переписку пользователя ради чистоты
        модели незачем. Вид записям там взять неоткуда, поэтому все они приходят
        знаниями, а маршрутизатор разложит их точнее при первом же обращении.
        """
        if self.store is None:
            return
        rows = self.store.notes(self.id, self.branch)
        if rows:
            self._long = memory.notes_from_rows(rows)
            self._long.version = max(int(row["version"] or 0) for row in rows)
            self._long.upto = max(int(row["upto"] or 0) for row in rows)
            self._long.at = max((row["at"] for row in rows), default=None)
            return
        legacy = self.store.facts(self.id, self.branch)
        if legacy:
            self._long = memory.notes_from_facts(legacy["content"], legacy["turn"], legacy["at"])
            self._long.version = int(legacy["version"] or 0)
            self._long.upto = int(legacy["upto"] or 0)
            logger.info("Агент [%s]: долговременная память поднята из блока фактов прошлой версии — %d зап.",
                        self.id, len(self._long.notes))
            return
        self._long = memory.LongTerm()

    def _load_persona(self) -> None:
        """Поднять профиль пользователя по ссылке из настроек.

        Профиль общий и лежит вне переписки, поэтому читается не из ветки, а из
        своей таблицы. Нет ссылки или профиль удалили — агент работает без
        профиля: это рабочее состояние, а не ошибка.
        """
        if self.store is None or not self.persona_id:
            self._persona = None
            return
        row = self.store.persona(self.persona_id)
        if row is None:
            logger.info("Агент [%s]: профиль «%s» не найден — работаем без профиля",
                        self.id, self.persona_id)
            self.persona_id = ""
            self._persona = None
            return
        self._persona = persona.Persona.from_row(row)

    def _load_task(self) -> None:
        """Поднять открытую задачу ветки: рабочая память тоже переживает перезапуск."""
        if self.store is None:
            return
        row = self.store.open_task(self.id, self.branch)
        self._task = memory.Task.from_row(row) if row else None

    def _load_summary(self) -> None:
        """Поднять действующую суммаризацию ветки из хранилища (последнюю версию)."""
        if self.store is None:
            return
        row = self.store.summary(self.id, self.branch)
        self._summary = Summary(
            text=row["content"],
            version=row["version"],
            upto=row["upto"],
            messages=row["folded_messages"],
            tokens=row["folded_tokens"],
            at=row["at"],
        ) if row else Summary()

    def _load_branch(self) -> None:
        """Найти описание активной ветки; нет такой — вернуться в основную."""
        if self.branch == MAIN_BRANCH or self.store is None:
            self.branch = MAIN_BRANCH
            self._branch = _main_branch()
            return
        row = next((b for b in self.store.branches(self.id) if b["id"] == self.branch), None)
        if row is None:
            logger.warning("Агент [%s]: ветки %d больше нет — открываем основную", self.id, self.branch)
            self.branch = MAIN_BRANCH
            self._branch = _main_branch()
            return
        self._branch = {"id": row["id"], "name": row["name"], "origin": row["origin"],
                        "shared": row["shared"], "fork_at": row["fork_at"]}

    def _last_seen(self) -> str:
        """Когда в памяти появилось последнее сообщение — для заметки модели."""
        moment = self.store.last_at(self.id, self.branch) if self.store is not None else None
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(moment)) if moment else "недавно"

    # ------------------------------------------------------------ управление им --

    def configure(
        self,
        model: str | None = None,
        temperature: float | None = None,
        memory_turns: int | None = None,
        tools_enabled: bool | None = None,
        planning: bool | None = None,
        max_steps: int | None = None,
        max_tokens: int | None = None,
        strategy: str | None = None,
        summarize: bool | None = None,
        summary_every: int | None = None,
        working: bool | None = None,
    ) -> None:
        """Изменить настройки агента.

        Проверки живут здесь, а не в интерфейсе: настройки — часть самого агента,
        и любой интерфейс поверх него получает их бесплатно.
        """
        if strategy is not None:
            if strategy not in config.STRATEGY_BY_CODE:
                raise AgentError(
                    f"Стратегия «{strategy}» неизвестна. Доступны: "
                    + ", ".join(s["code"] for s in config.STRATEGIES) + "."
                )
            self.strategy = strategy
            self._load_memory()  # стратегия сменилась: очереди собираются заново под её правила
        if working is not None:
            # Включили рабочую память на живом разговоре — маршрутизатору есть что
            # разбирать; выключили — карточка остаётся в базе, но из запроса уходит.
            self.working = bool(working)
            self._load_memory()
        if summarize is not None:
            self.summarize = bool(summarize)
            # Включили сжатие на живом агенте — всё, что за окном и ещё не свёрнуто,
            # встаёт в очередь и свернётся после следующего ответа; выключили —
            # очередь расходится, а сама суммаризация остаётся в базе.
            self._load_memory()
        if summary_every is not None:
            if not SUMMARY_EVERY_MIN <= summary_every <= SUMMARY_EVERY_MAX:
                raise AgentError(
                    f"Суммаризация обновляется, когда за окном памяти накопится N сообщений: "
                    f"N должно быть от {SUMMARY_EVERY_MIN} до {SUMMARY_EVERY_MAX}."
                )
            self.summary_every = int(summary_every)
        if model is not None:
            if not config.is_allowed_model(model):
                # Чужая модель — риск реальных списаний после free-квоты: в реестре
                # config.MODELS только модели со Stop-on-Exhaust.
                raise AgentError(f"Модель «{model}» не в безопасном списке. Выберите модель из списка.")
            self.model = model
            ceiling = config.model_max_output(model)
            if self.max_tokens and self.max_tokens > ceiling:
                # У новой модели потолок генерации может быть ниже — иначе первый же
                # вызов вернул бы «Range of max_tokens should be [1, N]».
                logger.info("Лимит ответа %d выше потолка модели — уменьшен до %d", self.max_tokens, ceiling)
                self.max_tokens = ceiling
        if temperature is not None:
            if not 0 <= temperature < 2:
                raise AgentError("Температура должна быть в диапазоне [0, 2).")
            self.temperature = float(temperature)
        if memory_turns is not None:
            if not 0 <= memory_turns <= MEMORY_TURNS_MAX:
                raise AgentError(
                    f"Глубина памяти должна быть от 0 до {MEMORY_TURNS_MAX} пар сообщений."
                )
            self.memory_turns = int(memory_turns)
            self._load_memory()  # глубину увеличили — доберём забытое из истории
        if tools_enabled is not None:
            self.tools_enabled = bool(tools_enabled)
        if planning is not None:
            self.planning = bool(planning)
        if max_steps is not None:
            if not 1 <= max_steps <= 12:
                raise AgentError("Потолок шагов должен быть от 1 до 12.")
            self.max_steps = int(max_steps)
        if max_tokens is not None:
            ceiling = config.model_max_output(self.model)
            if max_tokens and not 1 <= max_tokens <= ceiling:
                raise AgentError(
                    f"Лимит ответа должен быть от 1 до {ceiling} токенов: столько модель "
                    f"«{config.model_label(self.model)}» способна сгенерировать за раз."
                )
            self.max_tokens = int(max_tokens) or None   # ноль означает «без ограничения»
        logger.info(
            "Агент «%s» [%s]: настройки — модель=%s, t°=%s, память=%d пар, инструменты=%s, "
            "план=%s, шагов=%d, лимит ответа=%s, стратегия=%s, сжатие=%s (суммаризация каждые %d сообщ.), "
            "рабочая память=%s",
            self.profile.name, self.id, self.model, self.temperature, self.memory_turns,
            self.tools_enabled, self.planning, self.max_steps, self.max_tokens or "без ограничения",
            self.strategy, "вкл" if self.summarize else "выкл", self.summary_every,
            "вкл" if self.working else "выкл",
        )
        self.persist()  # настройки тоже переживают перезапуск

    # ----------------------------------------------- профиль пользователя --

    @property
    def persona(self) -> "persona.Persona | None":
        """Профиль, подключённый к запросам этого агента (None — без профиля)."""
        return self._persona

    def personas(self) -> list[dict]:
        """Все профили пользователя — их показывает интерфейс в переключателе."""
        if self.store is None:
            return [self._persona.to_dict()] if self._persona else []
        return [persona.Persona.from_row(row).to_dict() for row in self.store.personas()]

    def use_persona(self, persona_id: str) -> "persona.Persona | None":
        """Переключить профиль пользователя. Пустая строка — работать без профиля.

        Память и история при этом не меняются ни на байт: профиль лежит отдельно,
        поэтому один и тот же разговор можно продолжить с другими требованиями к
        ответам — ровно то, что задание просит проверить.
        """
        code = str(persona_id or "").strip()
        if code and self.store is not None and self.store.persona(code) is None:
            raise AgentError(f"Профиль «{code}» не найден. Выберите профиль из списка.")
        self.persona_id = code
        self._load_persona()
        self.persist()
        logger.info("Агент «%s» [%s]: профиль пользователя — %s", self.profile.name, self.id,
                    f"«{self._persona.name}» ({self._persona.summary()})" if self._persona
                    else "не подключён")
        return self._persona

    def edit_persona(self, values: dict) -> "persona.Persona":
        """Правка активного профиля руками: проверки те же, что у настроек агента."""
        if self._persona is None:
            raise AgentError("Профиль не подключён — править нечего.")
        try:
            changes = persona.apply_values(self._persona, values)
        except ValueError as e:
            raise AgentError(str(e)) from e
        if self.store is not None:
            self.store.save_persona(self._persona.row())
        logger.info("Агент «%s» [%s]: профиль «%s» правлен руками — %s",
                    self.profile.name, self.id, self._persona.name,
                    "; ".join(change.what for change in changes) or "без изменений")
        return self._persona

    def remove_persona(self, persona_id: str = "") -> None:
        """Удалить профиль насовсем. Переписка и память при этом не страдают.

        Агенты, которые на него ссылались, остаются без профиля — это рабочее
        состояние, а не поломка. Удалили последний профиль — при следующем запуске
        `load_personas` заведёт заготовки заново: пустой набор приложение понимает
        как первый запуск.
        """
        code = str(persona_id or self.persona_id).strip()
        if not code:
            raise AgentError("Профиль не выбран — удалять нечего.")
        if self.store is None:
            raise AgentError("Без хранилища профили нигде не лежат — удалять нечего.")
        self.store.remove_persona(code)
        if self.persona_id == code:
            self.persona_id = ""
            self._persona = None
            self.persist()
        logger.info("Агент «%s» [%s]: профиль «%s» удалён", self.profile.name, self.id, code)

    def create_persona(self, values: dict) -> "persona.Persona":
        """Завести новый профиль пользователя и сразу подключить его к агенту."""
        fresh = persona.Persona(id=uuid.uuid4().hex[:8], name="Профиль")
        try:
            persona.apply_values(fresh, values)
        except ValueError as e:
            raise AgentError(str(e)) from e
        if self.store is not None:
            self.store.save_persona(fresh.row())
        self.persona_id = fresh.id
        self._persona = fresh
        self.persist()
        logger.info("Агент «%s» [%s]: заведён профиль «%s» [%s]",
                    self.profile.name, self.id, fresh.name, fresh.id)
        return fresh

    def set_profile(
        self,
        name: str | None = None,
        role: str | None = None,
        instructions: str | None = None,
    ) -> None:
        """Сменить паспорт: имя, подпись роли и system-инструкцию.

        Память и история остаются — это тот же агент, просто теперь он ведёт себя
        иначе. Пустое поле означает «оставить как было», пустая инструкция —
        вернуться к инструкции по умолчанию.
        """
        current = self.profile
        self.profile = AgentProfile(
            name=(name if name is not None else current.name).strip()[:40] or current.name,
            role=(role if role is not None else current.role).strip() or current.role,
            instructions=(instructions if instructions is not None else current.instructions).strip()
            or DEFAULT_PROFILE.instructions,
        )
        self.persist()
        logger.info("Агент [%s]: паспорт обновлён — «%s», %s", self.id, self.profile.name, self.profile.role)

    def reset(self) -> None:
        """Забыть разговор целиком: все три слоя памяти, в оперативной и на диске.

        Именно все три, а не только диалог: долговременная память для того и
        отделена, чтобы переживать задачи и ветки, — значит забыть её можно только
        явно. Паспорт и настройки остаются: агент тот же самый, просто без прошлого.
        Стирать историю здесь важно, иначе после перезапуска забытое вернулось бы.

        Профиль пользователя здесь не трогаем: он описывает человека, а не разговор,
        и общий для всех агентов — стирать его вместе с перепиской значило бы
        заставить заново рассказывать о себе после каждой очистки.
        """
        self._memory.clear()
        self._pending_summary.clear()
        self._pending_route.clear()
        self._tool_routes.clear()
        self._summary = Summary()
        self._long = memory.LongTerm()
        self._task = None
        self.turns = 0
        self.branch = MAIN_BRANCH
        self._branch = _main_branch()
        if self.store is not None:
            self.store.forget(self.id)
        self.persist()
        logger.info("Агент «%s» [%s]: все слои памяти и история очищены", self.profile.name, self.id)

    # -------------------------------------------------------- рабочая память --

    def close_task(self) -> dict:
        """Завершить задачу: довести автомат до «Готово» по всем оставшимся этапам.

        Этапы агент проходит сам — по факту работы и по решению маршрутизатора, — и
        водить его за руку не нужно. Но у человека должно остаться одно честное
        действие: «всё, дело закрыто». Оно не ломает автомат и не прыгает через
        этапы, а прокручивает их по порядку, каждый — через ту же `transition`;
        в ленте видно весь пройденный путь. Итог задачи при этом переезжает в
        долговременную память.
        """
        if self._task is None or not self._task.open:
            raise AgentError("Открытой задачи нет — завершать нечего.")
        moved = []
        for target in memory.path_to_done(self._task.state):
            try:
                step = memory.transition(self._task, target, self.turns)
            except memory.TransitionError as e:      # линейный путь всегда разрешён
                raise AgentError(str(e)) from e
            if step:
                moved.append(step)
        handed = memory.handoff(self._task, self._long, self.turns)
        card = self._task.to_dict()
        self._persist_task()
        logger.info("Агент «%s» [%s]: задача «%s» завершена вручную — %s%s",
                    self.profile.name, self.id, self._task.title, " · ".join(moved) or "уже на месте",
                    f" · в долговременную: {handed.what}" if handed else "")
        return {"task": card, "moved": moved, "handoff": handed.what if handed else ""}

    def pause_task(self) -> dict:
        """Отложить задачу: автомат замирает на текущем этапе.

        Пауза возможна на любом этапе и этап не меняет — в этом и разница между
        «отложили» и «вернулись назад». Пока она стоит, карточка уходит из запроса
        (остаётся закладка в строку), правила рабочей памяти молчат, а любой
        переход — хоть от модели, хоть от кнопки — получает отказ.
        """
        if self._task is None or not self._task.open:
            raise AgentError("Открытой задачи нет — откладывать нечего.")
        try:
            moved = memory.pause(self._task, self.turns)
        except memory.TransitionError as e:
            raise AgentError(str(e)) from e
        if not moved:
            raise AgentError("Задача уже на паузе.")
        self._persist_task()
        logger.info("Агент «%s» [%s]: задача «%s» — %s",
                    self.profile.name, self.id, self._task.title, moved)
        return {"task": self._task.to_dict(), "moved": moved}

    def resume_task(self) -> dict:
        """Продолжить отложенную задачу с того же места.

        Карточка возвращается в запрос целиком, а первым обращением после паузы в
        инструкцию уходит прямая просьба продолжить с текущего шага и не
        переспрашивать того, что уже записано, — это и есть «продолжение без
        повторных объяснений».
        """
        if self._task is None or not self._task.open:
            raise AgentError("Открытой задачи нет — продолжать нечего.")
        try:
            moved = memory.resume(self._task, self.turns)
        except memory.TransitionError as e:
            raise AgentError(str(e)) from e
        if not moved:
            raise AgentError("Задача не на паузе — она и так в работе.")
        self._persist_task()
        logger.info("Агент «%s» [%s]: задача «%s» — %s",
                    self.profile.name, self.id, self._task.title, moved)
        return {"task": self._task.to_dict(), "moved": moved}

    def _persist_task(self) -> None:
        """Записать карточку задачи и долговременную память вне обращения."""
        if self.store is None or self._task is None:
            return
        self.store.save_task(self.id, self._task.row(), self.branch)
        self.store.save_notes(self.id, {"version": self._long.version, "upto": self._long.upto,
                                        "rows": self._long.items()}, self.branch)

    def tasks(self) -> list[dict]:
        """Все задачи активной ветки: открытая и архив закрытых."""
        if self.store is None:
            return [self._task.to_dict()] if self._task else []
        return [memory.Task.from_row(row).to_dict() for row in self.store.tasks(self.id, self.branch)]

    def layers(self) -> list[dict]:
        """Карта памяти: что сейчас лежит в каждом слое, сколько это весит и кто им управляет.

        Ровно то, что спрашивает задание: «какие данные попадают в каждый слой».
        Считается по тому же состоянию, из которого собирается запрос, поэтому
        расхождению между картой и реальностью взяться неоткуда.
        """
        summary_block = self._summary_block()
        long_block = self._long_block() + self._invariants_block()
        task_block = self._task_block()
        history = self.history_size
        task = self._task
        stage = (f"этап «{config.state_label(task.state)}», шаг {task.step} из {task.total} · "
                 f"задача «{task.title}»: {task.size()} пункт(ов)"
                 if task is not None and task.open and task else "")
        if task is not None and task.open and task and task.paused:
            # На паузе состояние слоя описывает не карточку, а закладку: в запросе
            # сейчас только она, и по весу это видно.
            stage = (f"⏸ на паузе с обращения №{task.paused_turn} · этап "
                     f"«{config.state_label(task.state)}», шаг {task.step} из {task.total} · "
                     f"в запросе только закладка")
        kinds = ", ".join(f"{config.note_kind_label(kind)} {len(items)}"
                          for kind, items in self._long.by_kind().items())
        return [
            {
                **config.LAYER_BY_CODE["short"],
                "active": True,
                "items": len(self._memory),
                "tokens": tokens.measure_messages(self._memory, self.model)
                + tokens.measure_text(summary_block, self.model),
                "state": f"{len(self._memory)} сообщ. в окне из {history} в истории"
                + (f" · суммаризация №{self._summary.version} вместо {self._summary.messages} сообщ."
                   if summary_block else ""),
            },
            {
                **config.LAYER_BY_CODE["working"],
                "active": self.working,
                "items": self._task.size() if self._task and self._task.open else 0,
                "tokens": tokens.measure_text(task_block, self.model),
                "state": stage or ("задачи нет" if self.working else "слой выключен"),
            },
            {
                **config.LAYER_BY_CODE["long"],
                "active": self._long_on,
                "items": len(self._long.notes),
                "tokens": tokens.measure_text(long_block, self.model),
                "state": (f"{len(self._long.notes)} запис(и): {kinds}" if self._long.notes else
                          ("пусто" if self._long_on else "слой выключен")),
            },
        ]

    # ------------------------------------------------------------ ветки диалога --

    def checkpoint(self, name: str = "") -> dict:
        """Поставить точку ветвления после последнего сообщения активной ветки.

        Точка — место в истории, от которого создаются ветки. Обычно её ставит сам
        `fork()` в момент ветвления, и тогда она остаётся в истории как адрес: от неё
        можно отвести ещё одну ветку, когда разговор уже ушёл дальше. Разговор точка
        не меняет — только запоминает границу.
        """
        self._need_store("точек ветвления")
        upto = self.store.last_id(self.id, self.branch)
        if not upto:
            raise AgentError("Точка ветвления ставится в разговоре: в этой ветке пока нет сообщений.")
        count = self.store.count(self.id, self.branch)
        name = _clean_name(name) or f"Точка {len(self.checkpoints()) + 1}"
        number = self.store.add_checkpoint(self.id, self.branch, upto, count, name)
        logger.info("Агент «%s» [%s]: точка ветвления «%s» после %d сообщ. ветки «%s»",
                    self.profile.name, self.id, name, count, self._branch["name"])
        return {"id": number, "branch": self.branch, "upto": upto, "messages": count, "name": name}

    def fork(self, name: str = "", checkpoint: int | None = None) -> dict:
        """Создать ветку от точки ветвления и переключиться на неё.

        Без точки ветка отходит от текущего места: точка ставится тут же. Ветка
        получает копию общего начала разговора (сообщения до точки, а с ними
        суммаризацию и факты на тот момент) и дальше живёт независимо: её
        сообщения, суммаризации и факты в другие ветки не попадают.
        """
        self._need_store("веток")
        if checkpoint is None:
            point = self.checkpoint()
        else:
            point = next((c for c in self.checkpoints() if c["id"] == checkpoint), None)
            if point is None:
                raise AgentError("Такой точки ветвления нет — возможно, её ветка удалена.")
        name = _clean_name(name) or f"Ветка {len(self.store.branches(self.id)) + 1}"
        branch = self.store.fork(
            self.id, point["branch"], point["upto"], name, origin=point["name"], checkpoint=point["id"]
        )
        self.switch_branch(branch)
        logger.info("Агент «%s» [%s]: ветка «%s» [%d] от точки «%s» (%d общих сообщ.)",
                    self.profile.name, self.id, name, branch, point["name"], point["messages"])
        return {"id": branch, "name": name, "origin": point["name"], "shared": point["messages"]}

    def switch_branch(self, branch: int) -> None:
        """Переключиться на ветку: память, суммаризация и факты собираются из её истории."""
        self._need_store("веток")
        if not self.store.branch_exists(self.id, int(branch)):
            raise AgentError("Такой ветки нет.")
        self.branch = int(branch)
        self._load_branch()
        self._load_summary()
        self._load_long()
        self._load_task()
        self._load_memory()
        self.persist()   # следующий запуск откроет ту же ветку
        logger.info("Агент «%s» [%s]: активна ветка «%s» — %d сообщ. в памяти из %d",
                    self.profile.name, self.id, self._branch["name"], len(self._memory), self.history_size)

    def delete_branch(self, branch: int) -> None:
        """Удалить ветку вместе с её перепиской; основную удалить нельзя."""
        self._need_store("веток")
        if int(branch) == MAIN_BRANCH:
            raise AgentError("Основную ветку удалить нельзя — это сама история агента.")
        if int(branch) == self.branch:
            self.switch_branch(MAIN_BRANCH)
        self.store.remove_branch(self.id, int(branch))

    def branches(self) -> list[dict]:
        """Все ветки агента, начиная с основной; у активной `active` = True."""
        main = _main_branch()
        main["checkpoint"] = 0
        main["at"] = self.created_at
        main["messages"] = self.history_size if self.branch == MAIN_BRANCH else (
            self.store.count(self.id, MAIN_BRANCH) if self.store is not None else 0
        )
        rows = [main] + (self.store.branches(self.id) if self.store is not None else [])
        for row in rows:
            row["active"] = row["id"] == self.branch
        return rows

    def checkpoints(self) -> list[dict]:
        """Точки ветвления агента во всех ветках."""
        return self.store.checkpoints(self.id) if self.store is not None else []

    def _need_store(self, what: str) -> None:
        if self.store is None:
            raise AgentError(f"Без хранилища нет {what}: ветки живут в истории на диске.")

    # ------------------------------------------------- состояние между запусками --

    def state(self) -> dict:
        """Всё, чем агент является, кроме переписки: её ведёт сама история.

        Это то, что уходит в хранилище. Обратная операция — `restore()`.
        """
        return {
            "id": self.id,
            "created_at": self.created_at,
            "turns": self.turns,
            "branch": self.branch,
            "profile": {
                "name": self.profile.name,
                "role": self.profile.role,
                "instructions": self.profile.instructions,
            },
            "settings": {
                "model": self.model,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "memory_turns": self.memory_turns,
                "tools_enabled": self.tools_enabled,
                "planning": self.planning,
                "max_steps": self.max_steps,
                "strategy": self.strategy,
                "summarize": self.summarize,
                "summary_every": self.summary_every,
                "working": self.working,
                "persona": self.persona_id,
            },
        }

    @classmethod
    def restore(cls, state: dict, store: Store) -> "Agent":
        """Поднять агента из сохранённого состояния вместе с его памятью.

        К значениям из файла относимся как к чужим: чего нет — берём по
        умолчанию, модель вне безопасного реестра меняем на модель по умолчанию.
        Приложение должно запускаться даже с устаревшим или правленым файлом.
        """
        profile = state.get("profile") or {}
        settings = state.get("settings") or {}
        model = settings.get("model", config.DEFAULT_MODEL)
        if not config.is_allowed_model(model):
            logger.warning("В истории модель «%s» вне реестра — берём %s", model, config.DEFAULT_MODEL)
            model = config.DEFAULT_MODEL
        strategy, summarize = _strategy_and_summarize(settings)

        agent = cls(
            profile=AgentProfile(
                name=profile.get("name") or DEFAULT_PROFILE.name,
                role=profile.get("role") or DEFAULT_PROFILE.role,
                instructions=profile.get("instructions") or DEFAULT_PROFILE.instructions,
            ),
            model=model,
            temperature=float(settings.get("temperature", config.AGENT_TEMPERATURE)),
            max_tokens=settings.get("max_tokens", config.AGENT_MAX_TOKENS),
            memory_turns=int(settings.get("memory_turns", config.AGENT_MEMORY_TURNS)),
            tools_enabled=bool(settings.get("tools_enabled", config.AGENT_TOOLS)),
            planning=bool(settings.get("planning", config.AGENT_PLANNING)),
            max_steps=int(settings.get("max_steps", config.AGENT_MAX_STEPS)),
            strategy=strategy,
            summarize=summarize,
            summary_every=int(settings.get("summary_every") or config.AGENT_SUMMARY_EVERY),
            working=bool(settings.get("working", config.AGENT_WORKING)),
            # Ссылка на профиль: у агента из базы прошлой версии её нет, и он
            # продолжит работать без профиля, пока его не выберут.
            persona_id=str(settings.get("persona") or ""),
            id=state.get("id") or uuid.uuid4().hex[:8],
            created_at=float(state.get("created_at") or time.time()),
            turns=int(state.get("turns") or 0),
            branch=int(state.get("branch") or MAIN_BRANCH),
            store=store,
            restored=True,
        )
        agent._load_branch()    # сначала ветка: остальное читается из её истории
        agent._load_summary()   # суммаризация раньше окна: от её границы зависит, что войдёт в окно
        agent._load_long()      # долговременная память раньше очередей: от её границы зависит очередь
        agent._load_task()
        agent._load_persona()   # профиль вне переписки, но нужен до сборки первого запроса
        agent._load_memory()
        logger.info(
            "Агент «%s» [%s] восстановлен: %d обращений, ветка «%s», стратегия %s, сжатие %s · слои: "
            "краткосрочная %d сообщ. из %d в истории (суммаризация №%d заменяет %d сообщ.), "
            "рабочая %s, долговременная %d зап. · ждут суммаризации %d, ждут маршрутизации %d",
            agent.profile.name, agent.id, agent.turns, agent._branch["name"], agent.strategy,
            "вкл" if agent.summarize else "выкл", len(agent._memory), agent.history_size,
            agent._summary.version, agent._summary.messages,
            f"«{agent._task.title}»" if agent._task else "нет задачи", len(agent._long.notes),
            len(agent._pending_summary), len(agent._pending_route),
        )
        return agent

    def persist(self) -> None:
        """Записать состояние агента в хранилище (без хранилища — ничего не делаем)."""
        if self.store is not None:
            self.store.save_agent(self.state())

    def mark_active(self) -> None:
        """Запомнить, что разговор идёт с этим агентом: его и откроет следующий запуск."""
        if self.store is not None:
            self.store.set_active(self.id)

    def erase(self) -> None:
        """Убрать агента из хранилища: после перезапуска его не будет."""
        if self.store is not None:
            self.store.remove_agent(self.id)

    # --------------------------------------------------------- для интерфейса --

    @property
    def memory(self) -> list[dict]:
        """Копия памяти: интерфейс её показывает, но менять не может."""
        return [dict(m) for m in self._memory]

    @property
    def history_size(self) -> int:
        """Сколько сообщений активной ветки лежит в истории (в памяти — обычно меньше)."""
        return self.store.count(self.id, self.branch) if self.store is not None else len(self._memory)

    def transcript(self) -> list[dict]:
        """Вся переписка активной ветки: из истории, если она есть, иначе из памяти.

        Интерфейс рисует ленту именно отсюда, поэтому после перезапуска на экране
        оказывается весь прошлый разговор, а не только то, что уйдёт в модель.
        """
        if self.store is not None:
            return self.store.messages(self.id, self.branch)
        return self.memory

    def tokens_state(self) -> dict:
        """Во что обойдётся следующий запрос и сколько уже потрачено за всё время.

        Считается до всякого вызова: инструкция, блоки стратегии, окно памяти и
        схемы инструментов уже известны, а значит известен и вес контекста.
        Интерфейс показывает это полосой — видно, как разговор занимает окно
        модели и из чего он состоит. Отдельно считается ВСЯ переписка на диске:
        обычно она заметно больше контекста, и разница между «сохранено» и
        «уходит в модель» — это и есть цена памяти.
        """
        specs = self._specs()
        summary_block = self._summary_block()
        long_block = self._long_block() + self._invariants_block()
        task_block = self._task_block()
        persona_block = self._persona_block()
        breakdown = tokens.measure(
            self._system_prompt([]), self._memory, "", specs, self.model,
            summary=summary_block, long=long_block, task=task_block, persona=persona_block,
        )
        limit = self._context_limit()
        history = self.transcript()
        fix = tokens.calibration(self.model)
        folded = self._summary.tokens if summary_block else 0
        beyond, beyond_tokens = self._beyond_window(history)
        return {
            "model": self.model,
            "breakdown": breakdown.to_dict(),
            "parts": breakdown.parts(),
            "context_tokens": breakdown.total,
            "limit": limit,
            "reserve": self._answer_reserve(),
            "max_output": config.model_max_output(self.model),
            "fill": breakdown.total / limit if limit else 0.0,
            "window_messages": len(self._memory),
            "history_messages": len(history),
            "history_tokens": tokens.measure_history(history, self.model),
            "calibration": {
                "factor": round(fix.factor, 3),
                "samples": fix.samples,
                "error_pct": fix.error_pct,
                "last_estimated": fix.last_estimated,
                "last_actual": fix.last_actual,
            },
            "spent": self.spent(),
            # Стратегия и сжатие: что заменяют их блоки, что ждёт очередей, что осталось
            # за окном и сколько весил бы тот же запрос с полной историей как есть.
            "strategy": self.strategy,
            "strategy_label": config.strategy_label(self.strategy),
            "strategy_en": config.strategy_en(self.strategy),
            "summarize": self.summarize,
            "branch": self.branch,
            "branch_name": self._branch["name"],
            "branch_origin": self._branch["origin"],
            "branch_shared": self._branch["shared"],
            "summary_every": self.summary_every,
            "summary": self._summary.to_dict(),
            "summary_active": bool(summary_block),
            "summary_tokens": breakdown.summary,
            "folded_messages": self._summary.messages if summary_block else 0,
            "folded_tokens": folded,
            # Слои памяти: что в каждом лежит и во что он обходится в этом запросе.
            "layers": self.layers(),
            "working": self.working,
            "task": self._task.to_dict() if self._task else None,
            "task_active": bool(task_block),
            "task_tokens": breakdown.task,
            "long": self._long.to_dict(),
            "invariants": [note.to_dict() for note in self._long.invariants()],
            "long_active": bool(long_block),
            "long_tokens": breakdown.long,
            # Профиль пользователя: не слой памяти, но такая же часть запроса —
            # и стоит он в каждом обращении одинаково.
            "persona": self._persona.to_dict() if self._persona else None,
            "persona_active": bool(persona_block),
            "persona_tokens": breakdown.persona,
            "route_pending": len(self._pending_route),
            "pending_messages": len(self._pending_summary),
            "pending_tokens": tokens.measure_messages(self._pending_summary, self.model),
            "dropped_messages": beyond,
            "dropped_tokens": beyond_tokens,
            "uncompressed": (breakdown.total - breakdown.summary - breakdown.long - breakdown.task
                             + folded + beyond_tokens),
        }

    def summaries(self) -> list[dict]:
        """Все версии суммаризации активной ветки: как она росла вместе с разговором."""
        return self.store.summaries(self.id, self.branch) if self.store is not None else []

    def notes(self) -> list[dict]:
        """Долговременная память активной ветки записями: что, какого вида и кто положил."""
        return self._long.items()

    def spent(self) -> dict:
        """Итог по расходу токенов за всё время жизни агента (из хранилища)."""
        if self.store is None:
            return {"turns": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "total_tokens": 0, "cost_usd": 0.0, "llm_calls": 0}
        return self.store.usage_totals(self.id)

    def usage_log(self) -> list[dict]:
        """Расход по обращениям, по строке на обращение: из этого растёт диаграмма."""
        return self.store.usage(self.id) if self.store is not None else []

    def passport(self) -> dict:
        """Всё состояние агента одним словарём — то, что рисует интерфейс."""
        return {
            "id": self.id,
            "name": self.profile.name,
            "role": self.profile.role,
            "instructions": self.profile.instructions,
            "model": self.model,
            "model_label": config.model_label(self.model),
            "tier": config.model_tier(self.model),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "memory_turns": self.memory_turns,
            "memory_messages": len(self._memory),
            "memory": self.memory,
            "turns": self.turns,
            "created_at": self.created_at,
            "tools_enabled": self.tools_enabled,
            "planning": self.planning,
            "max_steps": self.max_steps,
            "strategy": self.strategy,
            "strategy_label": config.strategy_label(self.strategy),
            "strategy_en": config.strategy_en(self.strategy),
            "summarize": self.summarize,
            "summary_every": self.summary_every,
            "summary": self._summary.to_dict(),
            "summary_active": bool(self._summary_block()),
            "working": self.working,
            "layers": self.layers(),
            "task": self._task.to_dict() if self._task else None,
            "task_active": bool(self._task_block()),
            "long": self._long.to_dict(),
            "invariants": [note.to_dict() for note in self._long.invariants()],
            "long_active": bool(self._long_block() or self._invariants_block()),
            # Персонализация: какой профиль подключён к каждому запросу и во что
            # он обходится. Список профилей интерфейс берёт отдельно (`personas`) —
            # в паспорте он был бы лишним походом в базу на каждую перерисовку.
            "persona": self._persona.to_dict() if self._persona else None,
            "persona_id": self.persona_id,
            "persona_active": self._persona is not None,
            "persona_tokens": tokens.measure_text(self._persona_block(), self.model),
            "states": config.TASK_STATES,
            "route_pending": len(self._pending_route),
            "pending_messages": len(self._pending_summary),
            "branch": self.branch,
            "branch_name": self._branch["name"],
            "branch_origin": self._branch["origin"],
            "branch_shared": self._branch["shared"],
            "tools": (tools.catalog() + memory.tool_catalog(self._long_on, self.working)
                      + persona.tool_catalog(self._persona is not None)),
            "workspace": str(tools.workspace()),
            # Память между запусками: сколько сохранено, где лежит и когда говорили
            "history_messages": self.history_size,
            "history_file": str(self.store.path) if self.store is not None else None,
            "last_seen_at": self.store.last_at(self.id, self.branch) if self.store is not None else None,
            "restored": self.restored,
        }


def load_agents(store: Store | None = None) -> tuple[list[Agent], Agent]:
    """Поднять агентов прошлого запуска и того из них, с кем шёл разговор.

    Это единственное место, где решается, откуда берутся агенты при старте, —
    интерфейсу остаётся показать готовое. Истории нет (первый запуск, стёрли файл)
    — заводим одного агента по умолчанию и сразу закрепляем его в хранилище.
    """
    store = store if store is not None else Store()
    agents = [Agent.restore(state, store) for state in store.agents()]
    if not agents:
        fresh = Agent(store=store)
        fresh.persist()
        fresh.mark_active()
        agents = [fresh]
        logger.info("История пуста — создан агент «%s» [%s]", fresh.profile.name, fresh.id)

    active_id = store.active_id()
    active = next((a for a in agents if a.id == active_id), agents[0])
    return agents, active


def load_personas(store: Store | None = None) -> list["persona.Persona"]:
    """Поднять профили пользователя, а при первом запуске — завести заготовки.

    Симметрично `load_agents`: это единственное место, где решается, откуда в
    приложении берутся профили. Заготовки пишутся в базу сразу, иначе ссылка на
    профиль у нового агента вела бы в пустоту.
    """
    store = store if store is not None else Store()
    rows = store.personas()
    if rows:
        return [persona.Persona.from_row(row) for row in rows]
    fresh = persona.presets()
    for item in fresh:
        store.save_persona(item.row())
    logger.info("Профилей в базе нет — заведены заготовки: %s",
                ", ".join(f"«{item.name}»" for item in fresh))
    return fresh


def _strategy_and_summarize(settings: dict) -> tuple[str, bool]:
    """Стратегия и сжатие из сохранённых настроек, с оглядкой на прошлые версии базы.

    Настройка `summarize` появилась, когда суммаризация перестала быть стратегией и
    стала опцией поверх любой из них. Пока её в базе нет (None — колонку только что
    дописали), значение выводим из того, что там лежало: в базе прошлой версии
    суммаризация была одним из положений переключателя стратегий, а ещё раньше —
    отдельным тумблером `compression`. Поведение агента от этого не меняется: он
    продолжает делать то же, что делал до обновления.
    """
    strategy = settings.get("strategy") or ""
    summarize = settings.get("summarize")
    if summarize is None:
        summarize = (strategy == "summary") if strategy else settings.get("compression", True)
    if strategy in ("summary", ""):
        strategy = "window"   # суммаризация поверх скользящего окна — это она и была
    if strategy not in config.STRATEGY_BY_CODE:
        logger.warning("В истории стратегия «%s» неизвестна — берём %s", strategy, config.AGENT_STRATEGY)
        strategy = config.AGENT_STRATEGY
    return strategy, bool(summarize)


def _extract_json(text: str) -> dict | None:
    """Достать JSON из ответа модели (на случай ```-обрамления или текста вокруг)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _task_action(before: dict | None, task: "memory.Task | None") -> str:
    """Что случилось с задачей за это обращение — одним словом для трассы."""
    if task is None or not task:
        return ""
    if before is None:
        return "открыта"
    if before["open"] and not task.open:
        return "закрыта"
    if not before["open"] and task.open:
        return "открыта"
    return "обновлена" if before != task.to_dict() else ""


def _clean_name(name: str) -> str:
    """Имя ветки или точки: одна строка без лишних пробелов, не длиннее NAME_MAX."""
    return " ".join((name or "").split())[:NAME_MAX]


def _short(value: object, limit: int = 60) -> str:
    """Короткое представление аргумента для лога."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _slim(message: dict) -> dict:
    """Сообщение истории в виде для памяти: роль, текст и номер строки, если есть."""
    slim = {"role": message["role"], "content": message["content"]}
    if message.get("id"):
        slim["id"] = message["id"]
    return slim
