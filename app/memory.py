"""Модель памяти агента: три слоя, три хранилища, один маршрутизатор.

До этого модуля память агента была одной кучей: окно последних сообщений, блок
фактов и суммаризация лежали рядом и различались только тем, как попадали в
запрос. Здесь она описана явно и разложена по слоям — у каждого своя роль, свой
срок жизни, своё место на диске и свой блок в инструкции модели:

    краткосрочная  (short)    текущий диалог: последние пары «вопрос-ответ» как
                              есть плюс суммаризация того, что из окна выпало.
                              Хранение: таблицы `messages` и `summaries`.
                              Живёт, пока идёт разговор в этой ветке.

    рабочая        (working)  данные текущей задачи: цель, шаги, находки,
                              созданные файлы, открытые вопросы — карточка
                              `Task`. Хранение: таблица `tasks`. Живёт, пока
                              задача открыта; закрыли — уходит в архив, а её итог
                              переезжает в долговременную память.

    долговременная (long)     профиль, решения, знания: записи `Note` вида
                              «ключ — значение», у каждой свой вид и источник.
                              Хранение: таблица `notes`. Переживает задачи,
                              сжатие истории и перезапуск приложения.

Главный вопрос такой модели — не «где хранить», а «что куда класть». Выбор здесь
явный и делается в трёх местах, и каждое видно в трассе под ответом:

    правило        код агента, без всякой модели: сообщения идут в краткосрочную,
                   план — в шаги задачи, выполненные инструменты — в находки,
                   записанный файл — в артефакты;
    маршрутизатор  отдельный вызов модели после ответа (`ROUTER_SYSTEM`): он
                   получает карточку задачи, текущую долговременную память и
                   новые сообщения, а возвращает и то и другое целиком —
                   изменившаяся запись заменяется, отменённая исчезает;
    инструмент     `remember` — агент сам кладёт что-то в слой прямо по ходу
                   работы, и это обычный шаг в трассе; `recall` — достаёт.

Модуль ничего не знает ни про API модели, ни про базу, ни про интерфейс: на входе
словари и текст, на выходе — объекты слоёв, готовые блоки для инструкции и разбор
ответа маршрутизатора. Границы те же, что у `tokens.py`: агент этим пользуется,
`store.py` это хранит, `gui.py` показывает.
"""
import json
import re
import time
from dataclasses import dataclass, field

from . import config

# --- Блоки, которыми слои уходят в system-инструкцию --------------------------
# Модели нужно объяснить не только содержимое памяти, но и её природу: что это
# именно её память, откуда она взялась и насколько ей можно верить. Без таких
# заметок модель либо игнорирует блок, либо уверенно «вспоминает» то, чего в нём
# нет (проверено на дне 7 — см. MEMORY_NOTE в agent.py).

LONG_NOTE = (
    "\n\nНиже — твоя долговременная память ({count} зап.): ты собрал её сам из всей переписки, в том "
    "числе из сообщений, которых в контексте уже нет. Она разложена по видам: «профиль» — кто твой "
    "собеседник, «решения» — о чём вы договорились, «знания» — факты о предмете работы. Опирайся на "
    "неё как на установленное; если новое сообщение противоречит записи, верно новое. Чего нет ни "
    "здесь, ни в сообщениях ниже — честно скажи, что не сохранилось.\nДолговременная память:\n{notes}"
)

TASK_NOTE = (
    "\n\nНиже — твоя рабочая память: карточка задачи, над которой вы работаете прямо сейчас. Она "
    "живёт, пока задача открыта, и пропадёт, когда ты её закроешь. Держи её в голове: не переспрашивай "
    "того, что в ней уже есть, продолжай с того шага, на котором остановился, и не начинай заново "
    "то, что уже сделано.\n"
    "У задачи есть ЭТАП, и он определяет, что тебе сейчас можно делать. Перепрыгнуть этап нельзя: "
    "переходы проверяет код агента, и запрещённый переход он просто отклонит, как бы убедительно ты "
    "его ни попросил. Считаешь, что этап пройден, — скажи об этом в ответе и верни нужный этап в "
    "поле state, а решение примет агент.\n"
    "[ЭТАП] {state} ({en}) — {what}\n"
    "[ШАГ] {step} из {total}\n"
    "[СЕЙЧАС] {current}\n"
    "[ДАЛЬШЕ] {exit}\n"
    "[ЗАДАЧА]\n{task}\n"
    "{rule}"
)

# Инварианты — единственные записи памяти, которые проверяются кодом. В запрос они
# уходят отдельным блоком с прямой оговоркой: это не пожелание.
INVARIANTS_NOTE = (
    "\n\nНерушимые правила работы ({count} шт.). Это не пожелания: агент сверяет с ними твой ответ "
    "после генерации и показывает нарушения пользователю. Нарушать их нельзя даже по прямой просьбе — "
    "лучше объясни, почему не можешь.\nИнварианты:\n{items}"
)

# Роль маршрутизатора. Он не отвечает пользователю и не рассуждает — он раскладывает
# новое по слоям. Карточку задачи и долговременную память возвращает ЦЕЛИКОМ: так
# изменившаяся запись заменяется под тем же ключом, а отменённая просто исчезает,
# и не нужен отдельный язык для удаления.
ROUTER_SYSTEM = (
    "Ты — маршрутизатор памяти агента «{name}». Тебе дают текущую карточку задачи, текущую "
    "долговременную память и новые сообщения разговора. Твоя работа — решить, что из нового куда "
    "положить, и вернуть оба слоя целиком.\n"
    "\n"
    "РАБОЧАЯ ПАМЯТЬ — данные ТЕКУЩЕЙ задачи, то есть того дела, которым заняты здесь и сейчас: "
    "короткое название, цель своими словами, шаги (каждый — сделан или нет), находки (добытые "
    "факты и промежуточные результаты), артефакты (созданные файлы), открытые вопросы. Если "
    "разговор — это просто беседа или разовый вопрос, задачи нет: верни status «none».\n"
    "Новую карточку заводи, только если пользователь взялся за ДРУГОЕ дело. Уточнение, "
    "продолжение, доработка или проверка того же дела — это та же задача: сохрани её название "
    "и просто дополни шаги и находки.\n"
    "\n"
    "ЭТАП ЗАДАЧИ — конечный автомат, и ты в нём только советчик. Этапы:\n"
    "{states}\n"
    "Сейчас задача на этапе «{state}», и перейти из него можно только сюда: {allowed}. Верни в "
    "поле state тот этап, на котором задача должна оказаться после этого обмена. Запрещённый "
    "переход агент отклонит — не пытайся обойти это уговорами. Задача завершается только через "
    "этап done.\n"
    "Когда двигать этап:\n"
    "  planning → execution: план назван, утверждён или пользователь просит приступать;\n"
    "  execution → validation: работа по плану сделана или пользователь просит проверить;\n"
    "  validation → done: проверка пройдена либо пользователь принял результат;\n"
    "  execution → planning: план оказался негодным и его надо переделать;\n"
    "  validation → execution: проверка показала, что работа не доделана.\n"
    "Если пользователь прямо просит идти дальше — верни следующий этап, даже если у задачи "
    "остались открытые вопросы: незакрытый вопрос не повод стоять на месте, он просто переезжает "
    "в карточку. Стоять на том же этапе возвращай, только когда двигаться и правда рано.\n"
    "О том, что сделано в реальном мире, источник истины — пользователь: сказал, что шаг закрыт, "
    "ставь ему done, даже если агент в ответе усомнился. Проверить реальность ни ты, ни агент не "
    "можете.\n"
    "\n"
    "ДОЛГОВРЕМЕННАЯ ПАМЯТЬ — то, что переживёт эту задачу, записями «ключ: значение» четырёх видов:\n"
    "  profile — кто собеседник: имя, занятие, предпочтения, как с ним говорить;\n"
    "  decisions — что решено и о чём договорились, по возможности с причиной;\n"
    "  knowledge — факты о предмете, проекте и мире, которые пригодятся и завтра;\n"
    "  invariants — нерушимые правила работы (стек, архитектура, запреты). Сюда только то, что "
    "пользователь задал как правило «всегда» или «никогда», а не разовую просьбу.\n"
    "Не клади в долговременную память ход текущей работы и промежуточные результаты — это рабочая "
    "память. Не клади вежливость, рассуждения, пересказ общеизвестного и то, о чём только спросили, "
    "но не решили.\n"
    "\n"
    # Место для правил, которые живут за пределами модели памяти: сейчас это
    # профиль пользователя (app/persona.py). Свои правила он приносит сам, а
    # маршрутизатор остаётся одним вызовом на обращение — второй стоил бы столько же.
    "{extra}"
    "Правила: ключ — короткое существительное или словосочетание (до четырёх слов); значение — одна "
    "короткая фраза; изменился факт — замени значение под тем же ключом; отменён — не возвращай "
    "этот ключ; ничего не выдумывай; не больше {limit} записей в долговременной памяти и не больше "
    "{items} пунктов в каждом списке задачи. Пиши на языке разговора.\n"
    "Верни СТРОГО JSON без пояснений и markdown:\n"
    '{{"task": {{"status": "open|none", "state": "planning|execution|validation|done", '
    '"title": "...", "goal": "...", "current": "над чем работаем прямо сейчас", '
    '"steps": [{{"text": "...", "done": true}}], "findings": ["..."], "artifacts": ["..."], '
    '"questions": ["..."]}}, '
    '"long": {{"profile": {{"ключ": "значение"}}, "decisions": {{}}, "knowledge": {{}}, '
    '"invariants": {{}}}}{schema}}}'
)

ROUTER_USER = (
    "Текущая задача:\n{task}\n\nТекущая долговременная память:\n{long}\n\n"
    "Новые сообщения ({count}):\n{messages}"
)

# Итог закрытой задачи переезжает в долговременную память отдельной записью: это и
# есть переток между слоями, ради которого рабочая память отделена от остальных.
HANDOFF_KEY = "итог задачи: {title}"


# --- Записи долговременной памяти --------------------------------------------

@dataclass
class Note:
    """Одна запись долговременной памяти: что запомнено, какого это вида и кто решил.

    `source` — самое интересное поле для задания дня: по нему видно, кто положил
    запись в слой (правило в коде, маршрутизатор, инструмент агента или переток из
    закрытой задачи), а значит видно и то, что выбор «что куда» действительно явный.
    """
    key: str
    value: str
    kind: str = "knowledge"
    source: str = "router"
    turn: int = 0
    at: float | None = None

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value, "kind": self.kind,
                "source": self.source, "turn": self.turn, "at": self.at}


@dataclass
class LongTerm:
    """Долговременный слой: записи «ключ — значение», разложенные по видам.

    Ключ уникален на весь слой, а не на вид: одно и то же не должно лежать
    «профилем» и «знанием» одновременно. Меняется вид — запись просто переезжает.
    """
    notes: dict[str, Note] = field(default_factory=dict)
    version: int = 0          # сколько раз слой обновлялся
    upto: int = 0             # id последнего сообщения истории, учтённого в слое
    at: float | None = None

    def __bool__(self) -> bool:
        return bool(self.notes)

    def by_kind(self) -> dict[str, list[Note]]:
        """Записи по видам, в порядке реестра видов: так они и уходят в модель."""
        groups: dict[str, list[Note]] = {kind["code"]: [] for kind in config.NOTE_KINDS}
        for note in self.notes.values():
            groups.setdefault(note.kind, []).append(note)
        return {kind: items for kind, items in groups.items() if items}

    def text(self) -> str:
        """Блок в том виде, в каком он уходит модели: заголовок вида, под ним записи.

        Инварианты сюда не входят: у них свой блок в инструкции и своя проверка
        после ответа, и смешивать закон с памятью о разговоре не стоит.
        """
        lines = []
        for kind, items in self.by_kind().items():
            if kind == "invariant":
                continue
            lines.append(f"[{config.note_kind_label(kind)}]")
            lines.extend(f"- {note.key}: {note.value}" for note in items)
        return "\n".join(lines)

    def invariants(self) -> list[Note]:
        """Нерушимые правила: единственные записи, которые проверяются кодом."""
        return [note for note in self.notes.values() if note.kind == "invariant"]

    def invariants_text(self) -> str:
        """Инварианты в том виде, в каком они уходят модели."""
        return "\n".join(f"- {note.key}: {note.value}" for note in self.invariants())

    def items(self) -> list[dict]:
        """Записи подряд, для интерфейса и хранилища."""
        return [note.to_dict() for note in self.notes.values()]

    def to_dict(self) -> dict:
        return {
            "notes": self.items(),
            "by_kind": {kind: [n.to_dict() for n in items] for kind, items in self.by_kind().items()},
            "count": len(self.notes),
            "text": self.text(),
            "version": self.version,
            "upto": self.upto,
            "at": self.at,
        }


# --- Карточка задачи (рабочая память) ----------------------------------------

@dataclass
class Task:
    """Рабочая память: состояние текущей задачи в виде конечного автомата.

    Карточка живёт, пока `status` == "open". Закрытая остаётся в архиве (по ней
    видно, чем агент занимался), но в запрос больше не уходит — в этом и разница
    между рабочей памятью и долговременной.

    `state` — этап автомата (`config.TASK_STATES`), и это не подпись, а рабочее
    поле: от этапа зависит, какие части карточки уйдут в запрос
    (`config.state_sections`) и что агенту сейчас разрешено делать. Менять его
    напрямую нельзя — только через `transition()`, которая сверяется с таблицей
    разрешённых переходов.
    """
    id: int = 0
    title: str = ""
    goal: str = ""
    status: str = "open"                                   # open | done
    state: str = config.AGENT_TASK_STATE                   # этап автомата
    current: str = ""                                      # над чем работаем прямо сейчас
    steps: list[dict] = field(default_factory=list)        # [{"text": ..., "done": bool}]
    findings: list[str] = field(default_factory=list)      # добытые факты и промежуточные результаты
    artifacts: list[str] = field(default_factory=list)     # созданные файлы
    questions: list[str] = field(default_factory=list)     # открытые вопросы
    turn: int = 0                                          # обращение, на котором задача открыта
    closed_turn: int = 0
    at: float | None = None
    updated_at: float | None = None

    def __bool__(self) -> bool:
        return bool(self.title or self.goal or self.steps or self.findings
                    or self.artifacts or self.questions)

    @property
    def open(self) -> bool:
        return self.status == "open"

    @property
    def total(self) -> int:
        """Сколько всего шагов в плане."""
        return len(self.steps)

    @property
    def step(self) -> int:
        """Номер текущего шага: первый невыполненный, а если всё сделано — последний."""
        for number, item in enumerate(self.steps, 1):
            if not item.get("done"):
                return number
        return len(self.steps)

    def current_step(self) -> str:
        """Над чем работаем прямо сейчас: явное значение или первый невыполненный шаг."""
        if self.current:
            return self.current
        for item in self.steps:
            if not item.get("done"):
                return item.get("text", "")
        return "план выполнен целиком" if self.steps else "план ещё не составлен"

    def text(self, sections: tuple | None = None) -> str:
        """Карточка в том виде, в каком она уходит модели.

        `sections` — какие части показывать (по умолчанию все). На этом и держится
        правило «инжектируй только нужное для текущего шага»: на планировании
        созданные файлы агенту не нужны, на проверке — наоборот, нужны все.
        """
        show = set(sections) if sections is not None else {
            "goal", "steps", "current", "findings", "artifacts", "questions"}
        lines = [f"Название: {self.title or '(без названия)'}"]
        if self.goal and "goal" in show:
            lines.append(f"Цель: {self.goal}")
        if self.steps and "steps" in show:
            lines.append("Шаги:")
            lines.extend(f"  {'[x]' if item.get('done') else '[ ]'} {item.get('text', '')}"
                         for item in self.steps)
        for key, caption, values in (("findings", "Находки", self.findings),
                                     ("artifacts", "Созданные файлы", self.artifacts),
                                     ("questions", "Открытые вопросы", self.questions)):
            if values and key in show:
                lines.append(f"{caption}:")
                lines.extend(f"  - {value}" for value in values)
        return "\n".join(lines)

    def summary(self) -> str:
        """Одна строка про задачу — для перетока в долговременную память и для шапки."""
        done = sum(1 for step in self.steps if step.get("done"))
        parts = [self.goal or self.title]
        if self.steps:
            parts.append(f"шагов {done} из {len(self.steps)}")
        if self.artifacts:
            parts.append("файлы: " + ", ".join(self.artifacts[:3]))
        return "; ".join(p for p in parts if p)

    def size(self) -> int:
        """Сколько всего пунктов в карточке — по этому числу видно, что слой не пуст."""
        return len(self.steps) + len(self.findings) + len(self.artifacts) + len(self.questions)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "status": self.status,
            "open": self.open,
            "state": self.state,
            "state_label": config.state_label(self.state),
            "state_en": config.state_en(self.state),
            "allowed": list(config.allowed_states(self.state)),
            "current": self.current_step(),
            "step": self.step,
            "total": self.total,
            "steps": [dict(step) for step in self.steps],
            "findings": list(self.findings),
            "artifacts": list(self.artifacts),
            "questions": list(self.questions),
            "turn": self.turn,
            "closed_turn": self.closed_turn,
            "at": self.at,
            "updated_at": self.updated_at,
            "text": self.text(),
            "summary": self.summary(),
            "size": self.size(),
        }

    @classmethod
    def from_row(cls, row: dict) -> "Task":
        """Карточка из строки хранилища: списки лежат там строками JSON."""
        state = row.get("state") or config.AGENT_TASK_STATE
        return cls(
            id=int(row.get("id") or 0),
            title=row.get("title") or "",
            goal=row.get("goal") or "",
            status=row.get("status") or "open",
            # К значению из базы относимся как к чужому: неизвестный этап мог
            # остаться от другой версии реестра, и на нём автомат бы застрял.
            state=state if state in config.TASK_STATE_BY_CODE else config.AGENT_TASK_STATE,
            current=row.get("current") or "",
            steps=[s for s in _load_list(row.get("steps")) if isinstance(s, dict)],
            findings=[str(v) for v in _load_list(row.get("findings"))],
            artifacts=[str(v) for v in _load_list(row.get("artifacts"))],
            questions=[str(v) for v in _load_list(row.get("questions"))],
            turn=int(row.get("turn") or 0),
            closed_turn=int(row.get("closed_turn") or 0),
            at=row.get("at"),
            updated_at=row.get("updated_at"),
        )

    def row(self) -> dict:
        """Карточка в виде строки хранилища."""
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "status": self.status,
            "state": self.state,
            "current": self.current,
            "steps": json.dumps(self.steps, ensure_ascii=False),
            "findings": json.dumps(self.findings, ensure_ascii=False),
            "artifacts": json.dumps(self.artifacts, ensure_ascii=False),
            "questions": json.dumps(self.questions, ensure_ascii=False),
            "turn": self.turn,
            "closed_turn": self.closed_turn,
        }


# --- Переходы автомата: единственное место, где решается этап ------------------

class TransitionError(Exception):
    """Запрошенный переход по этапам запрещён таблицей переходов.

    Отдельное исключение, а не просто False: запрет — это событие, о котором надо
    рассказать и модели, и пользователю, а не молча проигнорировать.
    """


def transition(task: Task, target: str, turn: int = 0) -> str:
    """Перевести задачу на другой этап — или отказать, если переход запрещён.

    Это и есть разница между автоматом и просьбой в инструкции. Модель может
    попросить любой этап, и попросит: она услужлива по природе и охотно
    согласится «пропустить планирование». Но применяет переход код, и только если
    он есть в `config.TASK_TRANSITIONS`. Текстовое правило в промпте при этом
    остаётся — как первая линия, а не как единственная.

    Возвращает человекочитаемое описание перехода; при запрете бросает
    `TransitionError` с объяснением, куда из текущего этапа перейти можно.
    """
    target = str(target or "").strip().lower()
    if target not in config.TASK_STATE_BY_CODE:
        raise TransitionError(
            f"Этапа «{target}» не существует. Есть: "
            + ", ".join(s["code"] for s in config.TASK_STATES) + "."
        )
    if target == task.state:
        return ""
    allowed = config.allowed_states(task.state)
    if target not in allowed:
        where = ", ".join(f"«{config.state_label(code)}»" for code in allowed) if allowed else "никуда"
        raise TransitionError(
            f"Переход «{config.state_label(task.state)}» → «{config.state_label(target)}» запрещён: "
            f"из этого этапа можно только {where}. Этапы нельзя перепрыгивать."
        )
    was = task.state
    task.state = target
    task.updated_at = time.time()
    if target == "done":
        # Автомат и статус карточки — одно и то же событие: задача закрывается
        # только через этап done, и другого пути к status="done" нет.
        task.status = "done"
        task.closed_turn = turn
    return f"{config.state_label(was)} → {config.state_label(target)}"


def overdue_state(task: Task) -> str:
    """Этап, до которого задача уже доросла по факту работы (пусто — не доросла).

    Автомат не должен отставать от реальности. Маршрутизатор — советчик, и он
    консервативен: на живом прогоне задача висела на планировании, когда в карточке
    уже стояли выполненные шаги и созданные файлы. Отставание видно по объективным
    признакам, и код исправляет его сам, не спрашивая модель:

        планирование → выполнение, если хоть один шаг отмечен выполненным или
        появился артефакт: значит работа уже идёт, как бы этап ни назывался;
        выполнение → проверка, когда план есть и все его шаги закрыты: работать
        больше не по чему, остаётся сверить сделанное.

    А вот завершение (проверка → готово) код сам не делает никогда: «проверка
    прошла» — не наблюдаемый признак, и закрытая задача уходит из контекста. Это
    решение остаётся за маршрутизатором и за человеком.
    """
    if not task.open:
        return ""
    if task.state == "planning":
        if any(step.get("done") for step in task.steps) or task.artifacts:
            return "execution"
    elif task.state == "execution":
        if task.steps and all(step.get("done") for step in task.steps):
            return "validation"
    return ""


def path_to_done(state: str) -> list[str]:
    """Этапы, которые осталось пройти до «Готово», по порядку.

    Нужно, чтобы «завершить задачу» было ОДНИМ действием человека, а не походом по
    этапам руками. Порядок реестра линеен, и движение вперёд разрешено всегда,
    поэтому путь — это просто хвост списка этапов после текущего.
    """
    codes = [item["code"] for item in config.TASK_STATES]
    if state not in codes:
        return []
    return codes[codes.index(state) + 1:]


def states_listing() -> str:
    """Этапы автомата с их переходами — для инструкции маршрутизатора."""
    lines = []
    for state in config.TASK_STATES:
        allowed = config.allowed_states(state["code"])
        where = " → " + ", ".join(allowed) if allowed else " → (конец)"
        lines.append(f"  {state['code']} ({state['en']}) — {state['what']};{where}")
    return "\n".join(lines)


# --- Что куда положено: трасса маршрутизации ---------------------------------

@dataclass
class Route:
    """Одна запись в трассе маршрутизации: что, в какой слой и по чьему решению.

    Это ответ на вопрос задания «какие данные попадают в каждый слой» — не на
    словах, а строкой под ответом агента.
    """
    layer: str                # short / working / long
    action: str               # add / change / remove
    what: str                 # человекочитаемо: «профиль/имя: Сергей»
    source: str               # rule / router / tool / handoff
    kind: str = ""            # вид записи или пункта карточки

    def to_dict(self) -> dict:
        return {"layer": self.layer, "action": self.action, "what": self.what,
                "source": self.source, "kind": self.kind}


@dataclass
class MemoryUpdate:
    """Что случилось с памятью после ответа: сколько разобрано и что куда легло."""
    messages: int = 0              # сколько сообщений разобрал маршрутизатор
    version: int = 0               # версия долговременной памяти после обновления
    routes: list[Route] = field(default_factory=list)
    long_items: int = 0
    long_tokens: int = 0
    task_tokens: int = 0
    task: dict | None = None       # карточка задачи после обновления
    task_action: str = ""          # «открыта» / «обновлена» / «закрыта» / «»
    called: bool = False           # был ли вызов маршрутизатора (правила идут без него)
    # Переходов за одно обращение может быть несколько: агент успевает составить план,
    # выполнить его и дойти до проверки, пока пользователь ждёт один ответ. Хранить
    # только последний — значит скрыть половину пути, поэтому здесь список.
    moved: list[str] = field(default_factory=list)
    rejected: str = ""             # отклонённый переход: что попросила модель и почему нельзя
    elapsed_s: float = 0.0
    call_tokens: int = 0

    def __bool__(self) -> bool:
        return bool(self.routes or self.task_action or self.moved or self.rejected)

    def by_layer(self, layer: str) -> list[Route]:
        return [route for route in self.routes if route.layer == layer]

    def to_dict(self) -> dict:
        return {
            "messages": self.messages,
            "version": self.version,
            "routes": [route.to_dict() for route in self.routes],
            "long_items": self.long_items,
            "long_tokens": self.long_tokens,
            "task_tokens": self.task_tokens,
            "task": self.task,
            "task_action": self.task_action,
            "called": self.called,
            "moved": list(self.moved),
            "rejected": self.rejected,
            "elapsed_s": self.elapsed_s,
            "call_tokens": self.call_tokens,
        }


# --- Инструменты памяти: агент кладёт в слой сам ------------------------------
# Схемы объявлены здесь, а исполняет их сам агент (см. `_memory_tool` в agent.py):
# обычные инструменты из tools.py — чистые функции и про агента ничего не знают, а
# эти пишут в его собственную память. Граница сохраняется: описание рядом с моделью
# памяти, исполнение — у того, чью память меняют.

WORKING_KINDS = {
    "goal": "цель",
    "step": "шаг",
    "finding": "находка",
    "artifact": "файл",
    "question": "вопрос",
}

REMEMBER = {
    "type": "function",
    "function": {
        "name": "remember",
        "description": (
            "Положить важное в свою память, явно выбрав слой. layer=\"long\" — долговременная "
            "память (переживёт задачу): kind=profile про собеседника, kind=decision про принятое "
            "решение, kind=knowledge про факт предметной области, kind=invariant про нерушимое "
            "правило работы («всегда…», «никогда…»); нужен короткий key. "
            "layer=\"working\" — рабочая память текущей задачи: kind=goal цель, step шаг, "
            "finding находка, artifact созданный файл, question открытый вопрос. Вызывай, когда "
            "прозвучало что-то, что понадобится позже; не клади сюда вежливость и общеизвестное."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": ["long", "working"],
                          "description": "Слой памяти: long — долговременная, working — текущая задача"},
                "kind": {"type": "string",
                         "enum": ["profile", "decision", "knowledge", "invariant",
                                  "goal", "step", "finding", "artifact", "question"],
                         "description": "Вид записи: для long — profile/decision/knowledge/invariant, "
                                        "для working — goal/step/finding/artifact/question"},
                "key": {"type": "string",
                        "description": "Короткий ключ записи (обязателен для layer=long)"},
                "value": {"type": "string", "description": "Само значение, одной короткой фразой"},
            },
            "required": ["layer", "kind", "value"],
        },
    },
}

RECALL = {
    "type": "function",
    "function": {
        "name": "recall",
        "description": (
            "Посмотреть, что лежит в твоей памяти. Без аргументов — все слои целиком; "
            "layer сужает до одного слоя (long или working), query ищет подстроку по ключам и "
            "значениям. Вызывай, когда нужно свериться с тем, что уже запомнено."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": ["long", "working"],
                          "description": "Какой слой смотреть; пусто — все"},
                "query": {"type": "string", "description": "Что искать; пусто — показать всё"},
            },
            "required": [],
        },
    },
}

TOOL_SPECS = [REMEMBER, RECALL]
TOOL_NAMES = {spec["function"]["name"] for spec in TOOL_SPECS}
TOOL_TITLES = {"remember": "запись в память", "recall": "чтение памяти"}

# Инструментам памяти нужна отдельная строчка в инструкции: без неё модель знает
# схему, но не понимает, зачем ей класть что-то в память, если всё и так в контексте.
TOOLS_NOTE = (
    "\n\nУ тебя есть память слоями, и ты можешь класть в неё сам: remember(layer=\"long\", …) — "
    "то, что понадобится и после этой задачи (кто твой собеседник, что решили, важные факты), "
    "remember(layer=\"working\", …) — данные текущей задачи (цель, шаги, находки, созданные файлы, "
    "открытые вопросы). Сомневаешься, помнишь ли ты что-то, — вызови recall. Контекст не вечен: "
    "то, что не положено в память, забудется вместе с окном диалога."
)


def tool_specs(long: bool, working: bool) -> list[dict]:
    """Схемы инструментов памяти для включённых слоёв (выключенных не обещаем)."""
    if not long and not working:
        return []
    return list(TOOL_SPECS)


def tool_catalog(long: bool, working: bool) -> list[dict]:
    """Инструменты памяти для интерфейса — рядом с обычными, в том же формате."""
    return [{"name": spec["function"]["name"],
             "title": TOOL_TITLES.get(spec["function"]["name"], spec["function"]["name"]),
             "description": spec["function"]["description"]}
            for spec in tool_specs(long, working)]


def apply_remember(
    arguments: dict,
    long: LongTerm,
    task: Task | None,
    turn: int,
    long_on: bool,
    working_on: bool,
) -> tuple[str, Route]:
    """Исполнить `remember`: положить значение в слой и сказать, что получилось.

    Возвращает текст результата для модели и запись трассы. Ошибку поднимает
    `ValueError` — агент превратит её в обычный результат инструмента, как делает
    с любой другой ошибкой: модель увидит её и попробует иначе.
    """
    layer = str(arguments.get("layer") or "").strip()
    kind = str(arguments.get("kind") or "").strip()
    key = _clean(arguments.get("key"), 60)
    value = _clean(arguments.get("value"), 200)
    if not value:
        raise ValueError("Нечего запоминать: значение пустое.")

    if layer == "long":
        if not long_on:
            raise ValueError("Долговременная память выключена: включите стратегию «Факты».")
        if kind not in config.NOTE_KIND_BY_CODE:
            raise ValueError("Для долговременной памяти kind — profile, decision или knowledge.")
        if not key:
            raise ValueError("Для долговременной памяти нужен короткий key.")
        before = long.notes.get(key)
        long.notes[key] = Note(key=key, value=value, kind=kind, source="tool", turn=turn, at=time.time())
        _trim_notes(long)
        action = "change" if before and before.value != value else ("keep" if before else "add")
        what = f"{config.note_kind_label(kind)}/{key}: {value}"
        if action == "change":
            what = f"{config.note_kind_label(kind)}/{key}: {before.value} → {value}"
        return (f"Запомнено в долговременной памяти ({config.note_kind_label(kind)}): {key} — {value}.",
                Route(layer="long", action=action, what=what, source="tool", kind=kind))

    if layer == "working":
        if not working_on:
            raise ValueError("Рабочая память выключена: включите тумблер «Рабочая память».")
        if kind not in WORKING_KINDS:
            raise ValueError("Для рабочей памяти kind — goal, step, finding, artifact или question.")
        if task is None:
            raise ValueError("Задачи сейчас нет: рабочую память некуда положить.")
        label = WORKING_KINDS[kind]
        if kind == "goal":
            task.goal = value
            if not task.title:
                task.title = _clean(value, 60)
        elif kind == "step":
            task.steps.append({"text": value, "done": False})
            del task.steps[config.TASK_ITEMS_LIMIT:]
        else:
            bucket = {"finding": task.findings, "artifact": task.artifacts,
                      "question": task.questions}[kind]
            if value not in bucket:
                bucket.append(value)
                del bucket[config.TASK_ITEMS_LIMIT:]
        task.updated_at = time.time()
        return (f"Записано в рабочую память задачи ({label}): {value}.",
                Route(layer="working", action="add", what=f"{label}: {value}", source="tool", kind=kind))

    raise ValueError("Слой памяти — long (долговременная) или working (рабочая).")


def apply_recall(arguments: dict, long: LongTerm, task: Task | None) -> str:
    """Исполнить `recall`: показать модели, что лежит в её памяти."""
    layer = str(arguments.get("layer") or "").strip()
    query = str(arguments.get("query") or "").strip().lower()
    blocks = []

    if layer in ("", "long"):
        notes = [n for n in long.notes.values()
                 if not query or query in n.key.lower() or query in n.value.lower()]
        blocks.append("Долговременная память:\n" + (
            "\n".join(f"- [{config.note_kind_label(n.kind)}] {n.key}: {n.value}" for n in notes)
            if notes else "(пусто)"))
    if layer in ("", "working"):
        if task is None or not task:
            blocks.append("Рабочая память: задачи сейчас нет.")
        else:
            text = task.text()
            if query:
                lines = [line for line in text.splitlines() if query in line.lower()]
                text = "\n".join(lines) or "(ничего не нашлось)"
            blocks.append("Рабочая память:\n" + text)
    return "\n\n".join(blocks)


# --- Маршрутизатор: разбор ответа модели -------------------------------------

def router_messages(
    name: str,
    task: Task | None,
    long: LongTerm,
    batch: list[dict],
    extra_rules: str = "",
    extra_schema: str = "",
    extra_input: str = "",
) -> list[dict]:
    """Собрать запрос маршрутизатора: текущие слои, этап автомата и новые сообщения.

    Три `extra_*` — место для того, что маршрутизатор раскладывает помимо слоёв
    памяти (сейчас это профиль пользователя): правила, кусок JSON-схемы ответа и
    текущее состояние на вход. Строками, а не объектом, нарочно: модель памяти не
    должна знать, что там за сущность, — иначе граница между модулями исчезнет.
    """
    listing = "\n".join(
        f"[{'пользователь' if m['role'] == 'user' else 'агент'}] {m['content']}" for m in batch
    )
    state = task.state if task is not None and task.open else config.AGENT_TASK_STATE
    allowed = config.allowed_states(state)
    return [
        {"role": "system", "content": ROUTER_SYSTEM.format(
            name=name, limit=config.LONG_LIMIT, items=config.TASK_ITEMS_LIMIT,
            states=states_listing(), state=state,
            allowed=", ".join(allowed) if allowed else "никуда, задача завершена",
            extra=(extra_rules + "\n") if extra_rules else "",
            schema=(", " + extra_schema) if extra_schema else "")},
        {"role": "user", "content": ROUTER_USER.format(
            task=(f"этап {task.state}, шаг {task.step} из {task.total}\n" + task.text())
            if task is not None and task else "(задачи нет)",
            long=json.dumps(_long_payload(long), ensure_ascii=False) if long else "(пусто)",
            count=len(batch), messages=listing) + (f"\n\n{extra_input}" if extra_input else "")},
    ]


def parse_router(content: str) -> dict | None:
    """Разобрать ответ маршрутизатора в нормализованный вид.

    Возвращает `{"task": {...}|None, "long": {вид: {ключ: значение}}, "raw": {…}}`
    или None, если это вообще не разобрать: тогда агент оставит слои как были —
    сбой формата не повод стирать память. В `raw` лежит разобранный JSON целиком:
    из него свою часть берут те, кто ездит с маршрутизатором за компанию (профиль
    пользователя), а модель памяти про них по-прежнему ничего не знает.
    """
    data = extract_json(content)
    if not isinstance(data, dict):
        return None

    raw_long = data.get("long")
    if not isinstance(raw_long, dict):
        raw_long = {}
    long_items: dict[str, dict[str, str]] = {}
    aliases = {"profile": "profile", "decisions": "decision", "decision": "decision",
               "knowledge": "knowledge", "facts": "knowledge",
               "invariants": "invariant", "invariant": "invariant"}
    for raw_kind, values in raw_long.items():
        kind = aliases.get(str(raw_kind).strip().lower())
        if kind is None or not isinstance(values, dict):
            continue
        bucket = long_items.setdefault(kind, {})
        for key, value in values.items():
            name = _clean(key, 60).strip(" :-")
            text = _flatten(value)
            if name and text:
                bucket[name] = text

    return {"task": _parse_task(data.get("task")), "long": long_items, "raw": data}


def _parse_task(raw: object) -> dict | None:
    """Карточка задачи из ответа маршрутизатора; None — задачи нет.

    Поле `state` здесь — только ПРЕДЛОЖЕНИЕ этапа. Применит его `transition()`,
    сверившись с таблицей переходов; в карточку оно отсюда не попадает.
    """
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or "open").strip().lower()
    if status in ("none", "no", "нет", ""):
        return None
    steps = []
    for step in _as_list(raw.get("steps")):
        if isinstance(step, dict):
            text = _clean(step.get("text") or step.get("step"), 160)
            done = bool(step.get("done"))
        else:
            text, done = _clean(step, 160), False
        if text:
            steps.append({"text": text, "done": done})
    wanted = str(raw.get("state") or "").strip().lower()
    # Старое «status: done» тоже принимаем как просьбу закрыть задачу: это то же
    # самое, что переход на этап done, и отказ он получит по тем же правилам.
    if not wanted and status == "done":
        wanted = "done"
    card = {
        "status": "open",
        "state": wanted if wanted in config.TASK_STATE_BY_CODE else "",
        "title": _clean(raw.get("title"), 60),
        "goal": _clean(raw.get("goal"), 200),
        "current": _clean(raw.get("current"), 160),
        "steps": steps[:config.TASK_ITEMS_LIMIT],
        "findings": _clean_list(raw.get("findings")),
        "artifacts": _clean_list(raw.get("artifacts")),
        "questions": _clean_list(raw.get("questions")),
    }
    if not any(card[part] for part in
               ("title", "goal", "steps", "findings", "artifacts", "questions", "state")):
        return None
    return card


def merge_long(long: LongTerm, items: dict[str, dict[str, str]], turn: int) -> list[Route]:
    """Заменить долговременную память тем, что вернул маршрутизатор, и описать разницу.

    Блок приходит целиком, поэтому исчезнувший ключ означает «запись отменена» — но
    записи, положенные инструментом на этом же обращении, маршрутизатор мог не
    увидеть, и терять их нельзя: они моложе его входных данных.
    """
    before = {key: (note.kind, note.value) for key, note in long.notes.items()}
    fresh: dict[str, Note] = {}
    for kind, values in items.items():
        for key, value in values.items():
            fresh[key] = Note(key=key, value=value, kind=kind, source="router",
                              turn=turn, at=time.time())
    for key, note in long.notes.items():          # моложе входа маршрутизатора — сохраняем
        if note.turn >= turn and note.source == "tool" and key not in fresh:
            fresh[key] = note
    long.notes = fresh
    _trim_notes(long)

    routes = []
    for key, note in long.notes.items():
        if key not in before:
            routes.append(Route(layer="long", action="add", kind=note.kind, source=note.source,
                                what=f"{config.note_kind_label(note.kind)}/{key}: {note.value}"))
        elif before[key][1] != note.value:
            routes.append(Route(layer="long", action="change", kind=note.kind, source=note.source,
                                what=f"{config.note_kind_label(note.kind)}/{key}: "
                                     f"{before[key][1]} → {note.value}"))
    for key, (kind, _value) in before.items():
        if key not in long.notes:
            routes.append(Route(layer="long", action="remove", kind=kind, source="router",
                                what=f"{config.note_kind_label(kind)}/{key}"))
    return routes


def merge_task(task: Task, card: dict, turn: int) -> list[Route]:
    """Обновить содержимое карточки тем, что вернул маршрутизатор, и описать разницу.

    Этап здесь НЕ трогается: его двигает только `transition()` — иначе получилось
    бы второе место, где меняется состояние автомата, и таблица переходов перестала
    бы что-либо гарантировать.
    """
    routes = []
    if card["current"] and card["current"] != task.current:
        task.current = card["current"]
    if card["title"] and card["title"] != task.title:
        routes.append(Route(layer="working", action="change" if task.title else "add", kind="title",
                            source="router", what=f"название: {card['title']}"))
        task.title = card["title"]
    if card["goal"] and card["goal"] != task.goal:
        routes.append(Route(layer="working", action="change" if task.goal else "add", kind="goal",
                            source="router", what=f"цель: {card['goal']}"))
        task.goal = card["goal"]

    if card["steps"]:
        was = {step["text"]: bool(step.get("done")) for step in task.steps}
        for step in card["steps"]:
            if step["text"] not in was:
                routes.append(Route(layer="working", action="add", kind="step", source="router",
                                    what=f"шаг: {step['text']}"))
            elif was[step["text"]] != step["done"] and step["done"]:
                routes.append(Route(layer="working", action="change", kind="step", source="router",
                                    what=f"шаг выполнен: {step['text']}"))
        task.steps = card["steps"]

    for kind, caption, values, bucket in (
        ("finding", "находка", card["findings"], task.findings),
        ("artifact", "файл", card["artifacts"], task.artifacts),
        ("question", "вопрос", card["questions"], task.questions),
    ):
        for value in values:
            if value not in bucket:
                bucket.append(value)
                routes.append(Route(layer="working", action="add", kind=kind, source="router",
                                    what=f"{caption}: {value}"))
        del bucket[config.TASK_ITEMS_LIMIT:]

    task.updated_at = time.time()
    return routes


# --- Инварианты: правило, которое проверяет код, а не только просит промпт -----

# Запреты внутри инварианта: «стек: пишем на Python, запрещено: Kotlin, Java».
# Всё, что после маркера и до конца значения, — список через запятую.
_BAN_MARKERS = ("запрещено:", "нельзя:", "запрещены:", "запрещён:", "запрещена:")


def invariant_bans(value: str) -> list[str]:
    """Слова, запрещённые инвариантом (пусто, если правило без явного запрета).

    Формат нарочно простой и видимый пользователю: правило без маркера остаётся
    просьбой в промпте, правило с маркером становится проверяемым. Придумывать
    здесь разбор естественного языка не нужно — он был бы ненадёжен, а выглядел
    бы как гарантия.
    """
    low = (value or "").lower()
    for marker in _BAN_MARKERS:
        at = low.find(marker)
        if at >= 0:
            tail = value[at + len(marker):]
            return [word for word in (part.strip(" .;»«\"'") for part in tail.split(",")) if word]
    return []


# Строки, в которых агент как раз ОТКАЗЫВАЕТСЯ нарушать инвариант, из проверки
# исключаются: «я не могу показать код на Java» — это соблюдение правила, а не его
# нарушение, хотя запрещённое слово в строке есть. Без этого фильтра честный отказ
# засчитывался бы как нарушение — проверено на живом прогоне.
_REFUSAL_MARKERS = (
    "не могу", "не буду", "не стану", "нельзя", "запрещ", "инвариант", "правил",
    "вместо", "не использу", "не подход", "не соответству",
)


def check_invariants(answer: str, long: LongTerm) -> list[str]:
    """Сверить ответ агента с инвариантами: что именно нарушено.

    Это вторая линия защиты. Первая — текст инварианта в инструкции, но текст
    остаётся просьбой: модель может её нарушить, и без проверки этого никто не
    заметит. Здесь нарушение становится видимым фактом под ответом.

    Проверка нарочно простая: поиск запрещённых слов по границам слова, минус
    строки, в которых агент объясняет свой отказ. Семантики здесь нет и быть не
    должно — иначе это была бы ещё одна модель, которой тоже надо верить. Цена
    простоты честная: правило вида «пиши коротко» так не проверить, и в окне такое
    правило прямо помечено как непроверяемое.
    """
    if not answer:
        return []
    usable = "\n".join(
        line for line in answer.splitlines()
        if not any(marker in line.lower() for marker in _REFUSAL_MARKERS)
    )
    violations = []
    for note in long.notes.values():
        if note.kind != "invariant":
            continue
        hit = [word for word in invariant_bans(note.value) if _mentions(usable, word)]
        if hit:
            violations.append(f"«{note.key}»: в ответе встретилось {', '.join(hit)}")
    return violations


def _mentions(text: str, word: str) -> bool:
    """Есть ли слово в тексте как отдельное слово, а не как часть другого."""
    pattern = r"(?<![0-9A-Za-zЀ-ӿ_])" + re.escape(word) + r"(?![0-9A-Za-zЀ-ӿ_])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def handoff(task: Task, long: LongTerm, turn: int) -> Route | None:
    """Переток при закрытии задачи: её итог остаётся записью в долговременной памяти.

    Рабочая память эфемерна и уходит из запроса вместе с задачей, но сам факт «мы
    это сделали и вот чем кончилось» переживает задачу — поэтому он и переезжает в
    соседний слой, а не пропадает.
    """
    summary = task.summary()
    if not summary:
        return None
    # Название задачи бывает длинным, а ключ записи короткий: режем сам заголовок,
    # иначе в ключ не помещается даже слово «итог» и записи выглядят одинаково.
    key = HANDOFF_KEY.format(title=_clean(task.title or "без названия", 40))
    long.notes[key] = Note(key=key, value=_clean(summary, 200), kind="decision",
                           source="handoff", turn=turn, at=time.time())
    _trim_notes(long)
    return Route(layer="long", action="add", kind="decision", source="handoff",
                 what=f"{config.note_kind_label('decision')}/{key}: {_clean(summary, 200)}")


# --- Правила в коде: что попадает в слои без всякой модели --------------------

def rules_from_turn(task: Task, plan: list[str], steps: list) -> list[Route]:
    """Разложить по рабочей памяти то, что видно из самого обращения.

    Никакой модели здесь нет — это детерминированные правила агента: план стал
    шагами задачи, выполненный инструмент — находкой, записанный файл — артефактом.
    По ним видно, что маршрутизация не сводится к «спросим модель»: часть выбора
    сделана в коде раз и навсегда.
    """
    routes = []
    known = {step["text"] for step in task.steps}
    for item in plan:
        text = _clean(item, 160)
        if text and text not in known and len(task.steps) < config.TASK_ITEMS_LIMIT:
            task.steps.append({"text": text, "done": False})
            known.add(text)
            routes.append(Route(layer="working", action="add", kind="step", source="rule",
                                what=f"шаг: {text}"))

    for step in steps:
        if not getattr(step, "ok", False):
            continue
        if step.tool == "write_file":
            path = _clean(step.arguments.get("path"), 80)
            if path and path not in task.artifacts:
                task.artifacts.append(path)
                routes.append(Route(layer="working", action="add", kind="artifact", source="rule",
                                    what=f"файл: {path}"))
        elif step.tool not in TOOL_NAMES:
            finding = f"{step.title}: {_clean(step.result, 160)}"
            if finding not in task.findings and len(task.findings) < config.TASK_ITEMS_LIMIT:
                task.findings.append(finding)
                routes.append(Route(layer="working", action="add", kind="finding", source="rule",
                                    what=f"находка: {finding}"))
    if routes:
        task.updated_at = time.time()
    return routes


# --- Разбор чужих данных ------------------------------------------------------

def extract_json(text: str) -> dict | None:
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


def notes_from_rows(rows: list[dict]) -> LongTerm:
    """Долговременная память из строк хранилища."""
    long = LongTerm()
    for row in rows:
        key = _clean(row.get("key"), 60)
        if not key:
            continue
        kind = row.get("kind") or "knowledge"
        long.notes[key] = Note(
            key=key,
            value=_clean(row.get("value"), 200),
            kind=kind if kind in config.NOTE_KIND_BY_CODE else "knowledge",
            source=row.get("source") or "router",
            turn=int(row.get("turn") or 0),
            at=row.get("at"),
        )
    return long


def notes_from_facts(content: str, turn: int = 0, at: float | None = None) -> LongTerm:
    """Долговременная память из блока фактов прошлой версии приложения.

    В базе дня 10 слой лежал одним JSON-объектом «ключ: значение» без видов. Вид
    там взять неоткуда, поэтому все записи становятся знаниями — терять переписку
    пользователя ради чистоты модели незачем, а вид он поправит первым же
    обращением.
    """
    long = LongTerm()
    try:
        items = json.loads(content or "{}")
    except json.JSONDecodeError:
        items = {}
    if not isinstance(items, dict):
        return long
    for key, value in items.items():
        name = _clean(key, 60)
        text = _flatten(value)
        if name and text:
            long.notes[name] = Note(key=name, value=text, kind="knowledge",
                                    source="router", turn=turn, at=at)
    _trim_notes(long)
    return long


def _long_payload(long: LongTerm) -> dict:
    """Долговременная память в том виде, в каком её понимает маршрутизатор."""
    payload = {"profile": {}, "decisions": {}, "knowledge": {}, "invariants": {}}
    bucket = {"profile": "profile", "decision": "decisions", "knowledge": "knowledge",
              "invariant": "invariants"}
    for note in long.notes.values():
        payload[bucket.get(note.kind, "knowledge")][note.key] = note.value
    return payload


def _trim_notes(long: LongTerm) -> None:
    """Удержать долговременную память в пределах потолка: лишнее с конца."""
    if len(long.notes) > config.LONG_LIMIT:
        long.notes = dict(list(long.notes.items())[:config.LONG_LIMIT])


def _clean(value: object, limit: int) -> str:
    """Одна строка без лишних пробелов, не длиннее лимита."""
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _flatten(value: object) -> str:
    """Значение записи строкой: модель иногда присылает список или объект."""
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    return _clean(value, 200)


def _as_list(value: object) -> list:
    if isinstance(value, list):
        return value
    return [value] if value else []


def _clean_list(value: object) -> list[str]:
    items = []
    for item in _as_list(value):
        text = _flatten(item)
        if text and text not in items:
            items.append(text)
    return items[:config.TASK_ITEMS_LIMIT]


def _load_list(value: object) -> list:
    """Список из колонки хранилища: там он лежит строкой JSON."""
    if isinstance(value, list):
        return value
    try:
        loaded = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return loaded if isinstance(loaded, list) else []
