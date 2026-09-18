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
    "У перехода на следующий этап бывает УСЛОВИЕ (строка ниже). Пока оно не выполнено, код этап не "
    "сменит — ни по твоей просьбе, ни по кнопке. Просят пойти дальше, а условие не выполнено — "
    "скажи об этом прямо и назови, чего не хватает, вместо того чтобы обещать переход.\n"
    "Состояние задачи — три строки ниже: ЭТАП (в какой фазе дело), ШАГ (где именно внутри плана) "
    "и ЖДЁМ (что должно произойти дальше и от кого этого ждут). Ход твой — делай; ход "
    "пользователя — скажи, чего ждёшь, и не топчись на месте.\n"
    "[ЭТАП] {state} ({en}) — {what}\n"
    "[ШАГ] {step} из {total}\n"
    "[СЕЙЧАС] {current}\n"
    "[ЖДЁМ] {expect}\n"
    "[ДАЛЬШЕ] {exit}\n"
    "[УСЛОВИЕ] {gate}\n"
    "[ЗАДАЧА]\n{task}\n"
    "{resume}{rule}"
)

# Первый ответ после паузы. Строка живёт ровно одно обращение: дальше продолжение
# уже не первое, и напоминание только занимало бы место.
RESUME_NOTE = (
    "ПРОДОЛЖЕНИЕ ПОСЛЕ ПАУЗЫ: это первый ответ после перерыва. Карточка выше — всё, на чём вы "
    "остановились. Продолжай прямо с указанного шага: не пересказывай задачу заново, не "
    "переспрашивай того, что уже записано, и не начинай с чистого листа.\n"
)

# Что уходит в запрос ВМЕСТО карточки, пока задача отложена. Дело на паузе не
# должно занимать контекст в каждом сообщении, но и молчать о нём нельзя: иначе
# агент про отложенное просто не знает и вернуться к нему не предложит.
PAUSED_NOTE = (
    "\n\nОтложенная задача: {bookmark}. Она на паузе: карточки сейчас нет в контексте, работу по "
    "ней не продолжай и о её содержимом не догадывайся. Скажут «продолжаем» — карточка вернётся "
    "целиком и работа пойдёт с того же шага."
)

# Блок про запрещённый переход. Уходит в инструкцию ДО генерации, когда в самом
# запросе просят этап, которого код не даст: отказать должен ассистент своими
# словами, а не строка в интерфейсе после ответа.
GATE_NOTE = (
    "\n\nПОПЫТКА ПЕРЕПРЫГНУТЬ ЭТАП: в сообщении просят перевести задачу на этап «{target}», а код "
    "агента такой переход не выполнит — {reason}\n"
    "Это не предмет спора и не твой выбор: порядок этапов держит таблица переходов, и попытка уже "
    "отклонена — состояние задачи осталось прежним. Ответь честно: скажи, что перепрыгнуть этап "
    "нельзя, объясни причину своими словами и назови, что для этого нужно ({need}). Не делай вид, "
    "что перешёл, и не обещай перейти. По текущему этапу работать при этом продолжай."
)

# Коды условий переходов — плоским множеством, чтобы проверять чужие значения
# (строку из базы, ответ маршрутизатора) одной операцией.
_GATE_CODES = {guard["code"] for guard in config.TASK_GUARDS.values()}

# Строки, в которых про переход как раз говорят «не надо». Без них «пока не
# завершай» читалось бы как просьба завершить — та же оговорка, что у инвариантов.
_SKIP_MARKERS = ("не надо", "не нужно", "не стоит", "рано ", "пока не", "не завершай",
                 "не закрывай", "не пропускай", "нельзя", "не переходи")

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
    "ШАГИ — это план ПРЕДСТОЯЩЕЙ работы по задаче, а не отчёт о том, что агент успел сделать в "
    "последнем ответе. Составил план и сохранил его в файл — это ещё не выполнение задачи: сам "
    "план становится шагами, а «составить план» отдельным шагом не считается. Помечай шаг "
    "выполненным, только когда сделана та работа, ради которой задача заведена, и об этом сказал "
    "пользователь или это видно по результату инструмента.\n"
    "\n"
    "ЭТАП ЗАДАЧИ — конечный автомат, и ты в нём только советчик. Этапы:\n"
    "{states}\n"
    "Сейчас задача на этапе «{state}», и перейти из него можно только сюда: {allowed}. Верни в "
    "поле state тот этап, на котором задача должна оказаться после этого обмена. Запрещённый "
    "переход агент отклонит — не пытайся обойти это уговорами. Задача завершается только через "
    "этап done.\n"
    "\n"
    "УСЛОВИЯ ПЕРЕХОДОВ — у части переходов есть условие, и без него переход не применится, даже "
    "если он разрешён таблицей:\n"
    "{gates}\n"
    "Подтверждает условие ПОЛЬЗОВАТЕЛЬ, а твоё дело — услышать это и вернуть код в поле gates. "
    "«Утверждаю», «план принят», «приступаем», «давай делать» — это plan_approved. «Расхождений "
    "нет», «всё верно», «принимаю результат», «заверши» после проверки — это checked. Ничего "
    "такого в сообщениях не было — верни gates пустым: подтверждение придумывать нельзя, это не "
    "формальность, а разрешение пользователя двигаться дальше.\n"
    "Сейчас подтверждено: {gates_now}.\n"
    "{pause}"
    "Когда двигать этап:\n"
    "  planning → execution: план назван, утверждён или пользователь просит приступать;\n"
    "  execution → validation: работа по плану сделана или пользователь просит проверить;\n"
    "  validation → done: проверка пройдена либо пользователь принял результат;\n"
    "  execution → planning: план оказался негодным и его надо переделать;\n"
    "  validation → execution: проверка показала, что работа не доделана, ИЛИ пользователь просит "
    "приступить к шагу, доделать или продолжить работу — это возврат к выполнению, а не "
    "завершение.\n"
    "Завершай задачу (done) только тогда, когда пользователь принял РЕЗУЛЬТАТ работы. Просьба "
    "«приступай», «продолжай», «утверждаю план» — это движение вперёд по работе, а не её конец.\n"
    "Если пользователь прямо просит идти дальше — верни следующий этап, даже если у задачи "
    "остались открытые вопросы: незакрытый вопрос не повод стоять на месте, он просто переезжает "
    "в карточку. Стоять на том же этапе возвращай, только когда двигаться и правда рано.\n"
    "О том, что сделано в реальном мире, источник истины — пользователь: сказал, что шаг закрыт, "
    "ставь ему done, даже если агент в ответе усомнился. Проверить реальность ни ты, ни агент не "
    "можете.\n"
    "\n"
    "ПАУЗА — состояние поверх этапа, а не этап: задача откладывается целиком, этап и шаги при "
    "этом сохраняются. Верни paused: true, когда пользователь просит отложить дело, переключается "
    "на другое или говорит «вернёмся позже»; paused: false — когда просит продолжить отложенное "
    "(«продолжаем», «вернёмся к ролику»). Поле не упомянул — состояние паузы не меняется.\n"
    "ОЖИДАЕМОЕ ДЕЙСТВИЕ — expect: чей сейчас ход (who: agent, если дело за агентом, user, если "
    "за пользователем) и чего ждут (what — одна короткая фраза). Базовое значение агент считает "
    "сам по этапу и шагу; возвращай expect, только если из разговора видно что-то более "
    "конкретное («пользователь пришлёт логотип», «ждём ответа от монтажёра»).\n"
    "\n"
    "ДОЛГОВРЕМЕННАЯ ПАМЯТЬ — то, что переживёт эту задачу, записями «ключ: значение» четырёх видов:\n"
    "  profile — кто собеседник: имя, занятие, предпочтения, как с ним говорить;\n"
    "  decisions — что решено и о чём договорились, по возможности с причиной;\n"
    "  knowledge — факты о предмете, проекте и мире, которые пригодятся и завтра.\n"
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
    '"questions": ["..."], "paused": false, "gates": ["plan_approved"], '
    '"expect": {{"who": "agent|user", "what": "чего ждём дальше"}}}}, '
    '"long": {{"profile": {{"ключ": "значение"}}, "decisions": {{}}, "knowledge": {{}}}}'
    '{schema}}}'
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
        """Блок в том виде, в каком он уходит модели: заголовок вида, под ним записи."""
        lines = []
        for kind, items in self.by_kind().items():
            lines.append(f"[{config.note_kind_label(kind)}]")
            lines.extend(f"- {note.key}: {note.value}" for note in items)
        return "\n".join(lines)

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

    Состояние задачи — это тройка «этап · текущий шаг · ожидаемое действие»:
    `state` отвечает, в какой фазе дело; `step`/`current` — где именно внутри
    плана; `expect`/`expect_who` — что должно произойти дальше и от кого этого
    ждут. Третье поле не украшение: по нему видно, стоит ли дело из-за агента или
    из-за человека, и оно же держит разговор после паузы.

    `paused` — состояние ПОВЕРХ этапа, а не пятый этап: пауза возможна на любом
    этапе, и этап при этом сохраняется. Пока она стоит, автомат заморожен
    (`transition` откажет), карточка в запрос не уходит, а вместо неё едет
    короткая закладка `bookmark()`.

    `gates` — какие условия переходов уже выполнены («план утверждён», «проверка
    пройдена»). Таблица переходов отвечает, куда из этапа можно уйти, а этот
    список — когда: без него «нельзя делать реализацию до утверждённого плана»
    осталось бы просьбой в промпте. `log` — журнал попыток перехода вместе с
    отклонёнными: отказ ничего не меняет, и без записи от него не осталось бы следа.
    """
    id: int = 0
    title: str = ""
    goal: str = ""
    status: str = "open"                                   # open | done
    state: str = config.AGENT_TASK_STATE                   # этап автомата
    current: str = ""                                      # над чем работаем прямо сейчас
    expect: str = ""                                       # ожидаемое действие: чего ждём
    expect_who: str = ""                                   # от кого ждём: agent | user
    paused: bool = False                                   # задача отложена, автомат заморожен
    paused_turn: int = 0                                   # на каком обращении отложена
    resuming: bool = False                                 # следующий ответ — первый после паузы
    gates: list[str] = field(default_factory=list)         # выполненные условия переходов
    log: list[dict] = field(default_factory=list)          # журнал попыток перехода, включая отклонённые
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

    def expect_line(self) -> str:
        """Ожидаемое действие одной строкой: чей ход и чего ждут."""
        who = config.actor_label(self.expect_who)
        return f"{who} — {self.expect}" if who and self.expect else self.expect

    def bookmark(self) -> str:
        """Закладка вместо карточки, пока задача на паузе.

        Полная карточка на паузе в запрос не уходит: дело отложено, и платить за
        него контекстом в каждом сообщении незачем. Но и молчать нельзя — иначе
        агент про отложенное дело просто не знает и вернуться к нему не предложит.
        Закладка — компромисс ценой в пару десятков токенов: чем занимались, на
        каком этапе остановились и как продолжить.
        """
        where = (f"шаг {self.step} из {self.total}" if self.total else "плана ещё нет")
        return (f"«{self.title or self.goal or 'без названия'}», этап "
                f"«{config.state_label(self.state)}», {where}")

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
            "expect": self.expect,
            "expect_who": self.expect_who,
            "expect_who_label": config.actor_label(self.expect_who),
            "expect_line": self.expect_line(),
            "paused": self.paused,
            "paused_turn": self.paused_turn,
            "resuming": self.resuming,
            "bookmark": self.bookmark(),
            "gates": list(self.gates),
            "gate": gate_state(self),
            "act": act_for(self),
            "log": [dict(item) for item in self.log],
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
            expect=row.get("expect") or "",
            expect_who=row.get("expect_who") or "",
            paused=bool(row.get("paused")),
            paused_turn=int(row.get("paused_turn") or 0),
            resuming=bool(row.get("resuming")),
            # Отметка о выполненном условии — такое же чужое значение, как этап:
            # код, которого больше нет в реестре, держать в карточке незачем.
            gates=[str(v) for v in _load_list(row.get("gates")) if str(v) in _GATE_CODES],
            log=[dict(v) for v in _load_list(row.get("log")) if isinstance(v, dict)],
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
            "expect": self.expect,
            "expect_who": self.expect_who,
            "paused": int(self.paused),
            "paused_turn": self.paused_turn,
            "resuming": int(self.resuming),
            "gates": json.dumps(self.gates, ensure_ascii=False),
            "log": json.dumps(self.log[-config.TASK_LOG_LIMIT:], ensure_ascii=False),
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


@dataclass
class Blocked:
    """Попытка перевести задачу на этап, которого код не разрешит.

    Находка рубежа ДО генерации: её делает `screen_transition`, разбирая сам
    запрос, и стоит она ноль токенов. Нужна, чтобы «реакцию ассистента» на
    запрещённый переход давал ассистент, а не строка в интерфейсе задним числом.
    """
    current: str              # этап, на котором задача сейчас
    target: str               # куда просят перевести
    kind: str                 # jump (через этап) / guard (условие) / paused
    reason: str               # почему нельзя, человеческими словами
    need: str = ""            # что должно случиться, чтобы стало можно

    def what(self) -> str:
        return (f"«{config.state_label(self.current)}» → «{config.state_label(self.target)}»: "
                f"{self.reason}")

    def to_dict(self) -> dict:
        return {"current": self.current, "target": self.target, "kind": self.kind,
                "current_label": config.state_label(self.current),
                "target_label": config.state_label(self.target),
                "reason": self.reason, "need": self.need, "what": self.what()}


def gate(task: Task, source: str, target: str) -> tuple[str, str]:
    """Условие перехода: выполнено ли оно и, если нет, почему.

    Возвращает пару «чего не хватает» и «объяснение». Первое пусто — переход
    разрешён; `fact` — не хватает наблюдаемого признака (плана ещё нет, шаги не
    закрыты), и одним сообщением этого не изменишь; `mark` — признак есть, но
    подтверждения не было.

    Разделение не формальное: на нём держится честность рубежа до генерации.
    «Приступаем» — это и просьба о переходе, и само утверждение плана, поэтому
    ругаться на отсутствие отметки ДО ответа нельзя: её поставит этот же обмен.
    А вот утвердить план, которого нет, нельзя никаким сообщением.
    """
    guard = config.guard_for(source, target)
    if not guard:
        return "", ""
    if not _fact_ready(task, guard["fact"]):
        return "fact", f"{guard['why']}. Сейчас не выполнено: {guard['need']}."
    if guard["code"] not in task.gates:
        return "mark", f"{guard['why']}. {guard['ask'][0].upper()}{guard['ask'][1:]}."
    return "", ""


def gate_state(task: Task) -> dict:
    """Условие выхода с текущего этапа — для интерфейса и трассы."""
    act = config.state_act(task.state)
    target = act.get("to", "")
    guard = config.guard_for(task.state, target) if target else {}
    if not guard:
        return {}
    kind, why = gate(task, task.state, target)
    return {"code": guard["code"], "label": guard["label"], "target": target,
            "target_label": config.state_label(target), "need": guard["need"],
            "ask": guard["ask"], "ready": not kind, "kind": kind, "why": why,
            "done": guard["code"] in task.gates}


def gate_line(task: Task) -> str:
    """Условие перехода на следующий этап одной строкой — для инструкции агента."""
    info = gate_state(task)
    if not info:
        return "переход дальше без условий"
    if info["ready"]:
        return (f"{info['label']} — выполнено, переход на «{info['target_label']}» разрешён")
    return f"{info['label']} — НЕ выполнено. {info['why']}"


def act_for(task: Task) -> dict:
    """Что человек может сделать кнопкой, чтобы двинуть дело дальше (пусто — нечего).

    Одно действие на этап, и оно идёт по закону: подтвердить условие выхода и
    попросить переход. Прыгнуть им через этап нельзя — в этом вся разница с кнопкой
    «завершить задачу», которая была до дня 15.
    """
    if not task.open or task.paused:
        return {}
    act = config.state_act(task.state)
    if not act.get("to"):
        return {}
    guard = config.guard_for(task.state, act["to"])
    kind, why = gate(task, task.state, act["to"])
    return {"label": act["label"], "to": act["to"], "hint": act.get("hint", ""),
            "code": guard.get("code", ""),
            # Кнопка доступна, пока не хватает только подтверждения: утверждать
            # план, которого нет, незачем — и объяснение к этому прилагается.
            "ready": kind != "fact", "why": why if kind == "fact" else ""}


def transition(task: Task, target: str, turn: int = 0, source: str = "router") -> str:
    """Перевести задачу на другой этап — или отказать, если переход запрещён.

    Это и есть разница между автоматом и просьбой в инструкции. Модель может
    попросить любой этап, и попросит: она услужлива по природе и охотно
    согласится «пропустить планирование». Но применяет переход код, и только если
    он есть в `config.TASK_TRANSITIONS` И выполнено условие перехода
    (`config.TASK_GUARDS`). Текстовое правило в промпте при этом остаётся — как
    первая линия, а не как единственная.

    Проверок три, и они разные: таблица отвечает «куда можно», условие — «когда
    можно», пауза — «сейчас нельзя никуда». Любая попытка, чем бы она ни кончилась,
    попадает в журнал карточки: отклонённый переход ничего не меняет, и без записи
    от него не осталось бы следа.

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
    if task.paused:
        # Пауза проверяется тем же кодом и тем же исключением, что и запрещённый
        # переход: иначе «замороженный автомат» был бы просто просьбой в промпте, а
        # модель (или кнопка) двигала бы этапы отложенной задачи как ни в чём не бывало.
        raise _refuse(
            task, target, turn, source,
            f"Задача на паузе ({task.bookmark()}): переход «{config.state_label(task.state)}» → "
            f"«{config.state_label(target)}» не применён. Сначала продолжите работу.",
        )
    allowed = config.allowed_states(task.state)
    if target not in allowed:
        where = ", ".join(f"«{config.state_label(code)}»" for code in allowed) if allowed else "никуда"
        raise _refuse(
            task, target, turn, source,
            f"Переход «{config.state_label(task.state)}» → «{config.state_label(target)}» запрещён: "
            f"из этого этапа можно только {where}. Этапы нельзя перепрыгивать.",
        )
    kind, why = gate(task, task.state, target)
    if kind:
        raise _refuse(
            task, target, turn, source,
            f"Переход «{config.state_label(task.state)}» → «{config.state_label(target)}» "
            f"не применён: {why}",
        )
    was = task.state
    task.state = target
    task.updated_at = time.time()
    # Отметка о подтверждении принадлежит этапу, с которого уводит. Вернулись на
    # этот этап — старое подтверждение больше не считается: план переигрывают, и
    # утверждать его придётся заново. Иначе галочка ставилась бы один раз навсегда.
    for code in config.guards_from(target):
        if code in task.gates:
            task.gates.remove(code)
    if target == "done":
        # Автомат и статус карточки — одно и то же событие: задача закрывается
        # только через этап done, и другого пути к status="done" нет.
        task.status = "done"
        task.closed_turn = turn
    moved = f"{config.state_label(was)} → {config.state_label(target)}"
    _log_move(task, was, target, turn, source, True, "")
    return moved


def approve(task: Task, code: str, turn: int = 0, source: str = "user") -> str:
    """Отметить, что условие перехода выполнено: план утверждён, проверка пройдена.

    Ставят её двое — человек кнопкой и маршрутизатор, услышав «утверждаю» или
    «расхождений нет», — и путь у обоих один, как у паузы. Подтвердить можно
    только условие выхода с ТЕКУЩЕГО этапа: «расхождений нет», сказанное на
    планировании, не значит ничего, и запасать галочки впрок нельзя.

    Возвращает описание события; пустая строка — отметка уже стояла.
    """
    code = str(code or "").strip()
    if code not in _GATE_CODES:
        raise TransitionError(f"Условия «{code}» не существует.")
    if not task.open:
        raise TransitionError("Задача завершена — подтверждать нечего.")
    if task.paused:
        raise TransitionError(
            f"Задача на паузе ({task.bookmark()}): подтверждать условия отложенной задачи нельзя. "
            "Сначала продолжите работу.")
    if code not in config.guards_from(task.state):
        owner = next((src for (src, _), guard in config.TASK_GUARDS.items()
                      if guard["code"] == code), "")
        codes = [item["code"] for item in config.TASK_STATES]
        if owner in codes and task.state in codes and codes.index(owner) < codes.index(task.state):
            # Подтверждение опоздало: этап, к которому оно относится, уже пройден.
            # Так и бывает в живом разговоре — «план утверждаю, приступай» код
            # разбирает сам, до ответа, а маршрутизатор возвращает то же самое
            # обращением позже. Отказывать тут не за что: условие своё дело сделало.
            return ""
        raise TransitionError(
            f"Условие «{_guard_by_code(code)['label']}» относится к этапу "
            f"«{config.state_label(owner)}», а задача сейчас на этапе "
            f"«{config.state_label(task.state)}».")
    guard = _guard_by_code(code)
    if not _fact_ready(task, guard["fact"]):
        raise TransitionError(
            f"Подтверждать нечего: {guard['need']} — этого ещё нет.")
    if code in task.gates:
        return ""
    task.gates.append(code)
    task.updated_at = time.time()
    return guard["label"]


def screen_transition(text: str, task: Task | None) -> "Blocked | None":
    """Рубеж ДО генерации: не просит ли сам ЗАПРОС запрещённого перехода.

    Стоит ноль токенов и срабатывает раньше всякой модели, поэтому агент успевает
    объяснить отказ своими словами в том же ответе, а не задним числом строкой в
    интерфейсе. Приём тот же, что у инвариантов дня 14, и та же оговорка: никакой
    семантики, только обороты из реестра этапов.

    Молчит, когда переход разрешён, — тогда и объяснять нечего, — и когда не
    хватает лишь подтверждения: его как раз и даёт это сообщение.
    """
    if not text or task is None or not task.open:
        return None
    target = _asked_state(text)
    if not target or target == task.state:
        return None
    if task.paused:
        return Blocked(current=task.state, target=target, kind="paused",
                       reason=f"задача отложена ({task.bookmark()}), автомат заморожен",
                       need="сначала вернуться к работе: «продолжаем»")
    allowed = config.allowed_states(task.state)
    if target not in allowed:
        where = ", ".join(f"«{config.state_label(code)}»" for code in allowed) if allowed else "никуда"
        return Blocked(current=task.state, target=target, kind="jump",
                       reason=f"через этап прыгать нельзя, из этого этапа можно только {where}",
                       need=f"пройти этапы по порядку: {' → '.join(config.state_label(c) for c in _road(task.state, target))}")
    kind, why = gate(task, task.state, target)
    if kind == "fact":
        guard = config.guard_for(task.state, target)
        return Blocked(current=task.state, target=target, kind="guard",
                       reason=why, need=guard["need"])
    return None


def screen_gates(text: str, task: Task | None) -> list[str]:
    """Условия, которые пользователь подтверждает прямо в этом запросе — ДО ответа.

    Тот же приём, что у рубежа переходов, и по той же причине: маршрутизатор
    работает после ответа, и без этой проверки «утверждаю, приступай» двигало бы
    этап с опозданием на целое обращение — агент успевал бы ответить «пока не
    могу, план не утверждён» на сообщение, которым его как раз утвердили.

    Подтверждается только условие выхода с ТЕКУЩЕГО этапа и только когда
    наблюдаемый признак уже есть: обещать «расхождений нет» на планировании
    бессмысленно, а утверждать нечего, пока нет плана.
    """
    if not text or task is None or not task.open or task.paused:
        return []
    usable = "\n".join(line for line in text.splitlines()
                       if not any(marker in line.lower() for marker in _SKIP_MARKERS))
    low = usable.lower().replace("ё", "е")
    found = []
    for code in config.guards_from(task.state):
        if code in task.gates:
            continue
        guard = _guard_by_code(code)
        if not _fact_ready(task, guard["fact"]):
            continue
        if any(phrase.replace("ё", "е") in low for phrase in guard.get("asks", ())):
            found.append(code)
    return found


def gate_note(blocked: "Blocked | None") -> str:
    """Блок инструкции про запрещённый переход (пусто, если запроса о нём не было)."""
    if blocked is None:
        return ""
    return GATE_NOTE.format(target=config.state_label(blocked.target),
                            reason=blocked.reason, need=blocked.need)


def note_attempt(task: Task, blocked: "Blocked", turn: int = 0, source: str = "user") -> None:
    """Записать в журнал попытку перехода, найденную в самом запросе.

    Рубеж до генерации до `transition()` не доходит — он разбирает просьбу заранее,
    и без этой записи самая наглядная попытка («переходи сразу в done») в журнале
    бы не осталась.
    """
    _log_move(task, blocked.current, blocked.target, turn, source, False, blocked.reason)


def _refuse(task: Task, target: str, turn: int, source: str, why: str) -> TransitionError:
    """Записать отклонённую попытку в журнал и вернуть готовое исключение."""
    _log_move(task, task.state, target, turn, source, False, why)
    return TransitionError(why)


def _log_move(task: Task, source_state: str, target: str, turn: int,
              source: str, ok: bool, why: str) -> None:
    """Запись в журнал переходов: кто, куда, получилось ли и почему нет."""
    task.log.append({"turn": turn, "from": source_state, "to": target, "ok": ok,
                     "source": source, "why": why, "at": time.time()})
    del task.log[:-config.TASK_LOG_LIMIT]


def _fact_ready(task: Task, fact: str) -> bool:
    """Наблюдаемый признак условия: то, что код видит сам, без слов и подтверждений."""
    if fact == "plan":
        # Хотя бы один шаг: утверждать нечего, пока плана нет вовсе. Потолок повыше
        # («не меньше двух») здесь не годится — задачу с одним шагом он запер бы на
        # планировании навсегда, а от пустых карточек защищает другое правило.
        return task.total >= 1
    if fact == "steps_done":
        return bool(task.steps) and all(step.get("done") for step in task.steps)
    return True


def _guard_by_code(code: str) -> dict:
    """Условие перехода по коду отметки."""
    return next((guard for guard in config.TASK_GUARDS.values() if guard["code"] == code), {})


def _road(source: str, target: str) -> list[str]:
    """Этапы по порядку от текущего до нужного — чтобы показать, что придётся пройти."""
    codes = [item["code"] for item in config.TASK_STATES]
    if source not in codes or target not in codes:
        return []
    start, finish = codes.index(source), codes.index(target)
    return codes[start + 1:finish + 1] if finish > start else codes[finish:start]


def _asked_state(text: str) -> str:
    """Этап, на который просят перевести задачу (пусто — просьбы не было).

    Берётся ПОСЛЕДНЕЕ совпадение по тексту: в «давай пропустим планирование и сразу
    считай задачу выполненной» просьб две, и настоящая — та, что в конце.
    """
    usable = "\n".join(line for line in text.splitlines()
                       if not any(marker in line.lower() for marker in _SKIP_MARKERS))
    low = usable.lower().replace("ё", "е")
    best, at = "", -1
    for state in config.TASK_STATES:
        for phrase in config.state_asks(state["code"]):
            found = low.rfind(phrase.replace("ё", "е"))
            if found > at:
                best, at = state["code"], found
    return best


def pause(task: Task, turn: int = 0) -> str:
    """Отложить задачу: автомат замирает на текущем этапе.

    Пауза — не этап и не возврат назад: этап, шаг и вся карточка остаются как
    есть, замирает только движение. Поэтому она и возможна на любом этапе, а
    «продолжить» не требует объяснять заново, чем занимались.

    Возвращает описание события; пустая строка — задача уже была на паузе.
    """
    if not task.open:
        raise TransitionError("Задача уже завершена — откладывать нечего.")
    if task.paused:
        return ""
    task.paused = True
    task.paused_turn = turn
    task.resuming = False
    task.updated_at = time.time()
    refresh_expect(task)
    return f"пауза на этапе «{config.state_label(task.state)}», шаг {task.step} из {task.total}"


def resume(task: Task, turn: int = 0) -> str:
    """Продолжить отложенную задачу с того же места.

    `resuming` — пометка «следующий ответ первый после паузы»: по ней в инструкцию
    попадает прямая просьба продолжить с текущего шага и не переспрашивать того,
    что уже есть в карточке. Снимает её агент в конце обращения — иначе просьба
    висела бы в каждом следующем запросе.
    """
    if not task.open:
        raise TransitionError("Задача завершена — продолжать нечего.")
    if not task.paused:
        return ""
    task.paused = False
    task.resuming = True
    task.updated_at = time.time()
    refresh_expect(task)
    return (f"продолжаем с этапа «{config.state_label(task.state)}», "
            f"шаг {task.step} из {task.total}")


def expected_action(task: Task) -> tuple[str, str]:
    """Ожидаемое действие: чей ход и чего ждут. Правило в коде, без модели.

    Ход у того, от кого зависит следующее движение автомата, а не у того, кто
    последним говорил:

        пауза              — ход человека: продолжить дело может только он;
        планирование       — плана ещё нет, ход агента: предложить его; план есть,
                             ход человека: утвердить или сказать «приступаем»;
        выполнение         — есть незакрытый шаг, ход агента: сделать его;
        проверка           — ход человека: подтвердить результат, потому что
                             завершение — единственный переход, который код сам не
                             делает никогда.
    """
    if not task.open:
        return "", "задача завершена"
    if task.paused:
        return "user", config.TASK_PAUSE_EXPECT
    if task.state == "planning":
        who = "user" if task.steps else "agent"
    elif task.state == "execution":
        who = "agent" if any(not step.get("done") for step in task.steps) else "user"
    else:
        who = "user"
    what = config.state_expect(task.state, who)
    if who == "agent" and task.state == "execution" and task.total:
        what = f"выполнить шаг {task.step} из {task.total}: {task.current_step()}"
    return who, what


def refresh_expect(task: Task) -> "Route | None":   # Route объявлен ниже — аннотация строкой
    """Пересчитать ожидаемое действие по состоянию карточки.

    Код пересчитывает его после каждого изменения задачи, поэтому поле никогда не
    пустует — даже если маршрутизатор промолчал или ответил не тем форматом.
    Уточнение от модели живёт до следующего пересчёта: наблюдаемый признак
    (закрылся шаг, сменился этап) важнее формулировки, придуманной ход назад.
    """
    who, what = expected_action(task)
    if (who, what) == (task.expect_who, task.expect):
        return None
    task.expect_who, task.expect = who, what
    task.updated_at = time.time()
    return Route(layer="working", action="change", kind="expect", source="rule",
                 what=f"ждём: {task.expect_line()}")


def overdue_state(task: Task) -> str:
    """Этап, до которого задача уже доросла по факту работы (пусто — не доросла).

    Автомат не должен отставать от реальности. Маршрутизатор — советчик, и он
    консервативен: на живом прогоне задача висела на планировании, когда в карточке
    уже стояли выполненные шаги и созданные файлы. Отставание видно по объективным
    признакам, и код исправляет его сам, не спрашивая модель:

        планирование → выполнение, когда план утверждён: отметка о подтверждении и
        есть тот самый наблюдаемый признак, а работа без неё не начинается — в этом
        и смысл условия «нельзя делать реализацию до утверждённого плана»;
        выполнение → проверка, когда план есть и все его шаги закрыты: работать
        больше не по чему, остаётся сверить сделанное;
        проверка → готово, когда результат принят: до дня 15 код этого перехода не
        делал никогда, потому что «проверка прошла» не было наблюдаемо. Теперь
        наблюдаемо: подтверждение — явная отметка в карточке, и ставит её человек
        или маршрутизатор с его слов, а не сам код.

    Условие перехода правило проверяет заранее (`gate`) и молчит, если оно не
    выполнено: правило не «хочет» этап, оно догоняет факт, и отказ от него засорял
    бы журнал попыток, где должны стоять настоящие попытки прыгнуть.

    На паузе правило молчит: отложенная задача не должна доезжать до следующего
    этапа сама, пока человек не вернулся к делу.
    """
    if not task.open or task.paused:
        return ""
    target = ""
    if task.state == "planning":
        target = "execution"
    elif task.state == "execution":
        if task.steps and all(step.get("done") for step in task.steps):
            target = "validation"
    elif task.state == "validation":
        target = "done"
    if not target or any(gate(task, task.state, target)):
        return ""
    return target


def states_listing() -> str:
    """Этапы автомата с их переходами и условиями — для инструкции маршрутизатора."""
    lines = []
    for state in config.TASK_STATES:
        allowed = config.allowed_states(state["code"])
        where = []
        for code in allowed:
            guard = config.guard_for(state["code"], code)
            where.append(f"{code} (только если {guard['label']})" if guard else code)
        tail = " → " + ", ".join(where) if where else " → (конец)"
        lines.append(f"  {state['code']} ({state['en']}) — {state['what']};{tail}")
    return "\n".join(lines)


def gates_listing() -> str:
    """Условия переходов и то, чем они подтверждаются, — для инструкции маршрутизатора."""
    lines = []
    for (src, dst), guard in config.TASK_GUARDS.items():
        lines.append(f"  {guard['code']} — {guard['label']} ({src} → {dst}); {guard['why']}")
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
    paused: str = ""               # событие паузы за обращение: отложили или вернулись
    rejected: str = ""             # отклонённый переход: что попросила модель и почему нельзя
    gates: list[str] = field(default_factory=list)   # условия, подтверждённые за обращение
    elapsed_s: float = 0.0
    call_tokens: int = 0

    def __bool__(self) -> bool:
        return bool(self.routes or self.task_action or self.moved or self.paused
                    or self.rejected or self.gates)

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
            "paused": self.paused,
            "rejected": self.rejected,
            "gates": list(self.gates),
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
            "решение, kind=knowledge про факт предметной области; нужен короткий key. "
            "Нерушимое правило работы («всегда…», «никогда…») сюда не клади — для него "
            "отдельный инструмент restrict. "
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
                         "enum": ["profile", "decision", "knowledge",
                                  "goal", "step", "finding", "artifact", "question"],
                         "description": "Вид записи: для long — profile/decision/knowledge, "
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
        if task.paused:
            raise ValueError("Задача на паузе: её карточка заморожена, пока работу не продолжат.")
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
    paused = task is not None and task.open and task.paused
    return [
        {"role": "system", "content": ROUTER_SYSTEM.format(
            name=name, limit=config.LONG_LIMIT, items=config.TASK_ITEMS_LIMIT,
            states=states_listing(), state=state, gates=gates_listing(),
            gates_now=(", ".join(_guard_by_code(code)["label"] for code in task.gates)
                       if task is not None and task.gates else "ничего"),
            allowed=", ".join(allowed) if allowed else "никуда, задача завершена",
            pause=("Задача СЕЙЧАС НА ПАУЗЕ: этап не двигай и карточку не переписывай. Просит "
                   "продолжить — верни paused: false, и работа пойдёт с того же места.\n"
                   if paused else ""),
            extra=(extra_rules + "\n") if extra_rules else "",
            schema=(", " + extra_schema) if extra_schema else "")},
        {"role": "user", "content": ROUTER_USER.format(
            task=(f"этап {task.state}, шаг {task.step} из {task.total}"
                  + (", НА ПАУЗЕ" if task.paused else "")
                  + (f", ждём: {task.expect_line()}" if task.expect else "")
                  + "\n" + task.text())
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
               "knowledge": "knowledge", "facts": "knowledge"}
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
    # Ожидаемое действие: объектом («кто» и «что») или просто строкой — модель
    # возвращает и так, и так, а поле слишком полезное, чтобы терять его из-за формы.
    raw_expect = raw.get("expect")
    if isinstance(raw_expect, dict):
        who = str(raw_expect.get("who") or "").strip().lower()
        what = _clean(raw_expect.get("what") or raw_expect.get("action"), 160)
    else:
        who, what = "", _clean(raw_expect, 160)
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
        "expect": what,
        "expect_who": who if who in config.TASK_ACTORS else "",
        # Что пользователь подтвердил в этом обмене: «утверждаю план», «расхождений
        # нет». Это не переход — это отметка, без которой переход не состоится.
        # Применяет её агент через `approve()`, и там же она может получить отказ.
        "gates": [code for code in (str(v).strip() for v in _as_list(raw.get("gates")))
                  if code in _GATE_CODES],
        # Пауза — поле с тремя значениями: да, нет и «маршрутизатор о ней не сказал».
        # Обычное булево с умолчанием False снимало бы паузу каждый раз, когда модель
        # просто забыла упомянуть поле, — а забывает она часто.
        "paused": _parse_flag(raw.get("paused")),
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
        # Инструмент агента и переток закрытой задачи: обе записи появляются в том же
        # обращении и маршрутизатору на вход не попадали. Итог задачи особенно важен —
        # с дня 15 задача закрывается ещё до его вызова, подтверждением из запроса.
        if note.turn >= turn and note.source in ("tool", "handoff") and key not in fresh:
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
    бы что-либо гарантировать. Пауза по той же причине применяется не здесь, а
    через `pause()`/`resume()`.

    А вот ожидаемое действие маршрутизатор уточнить может: базовое значение код
    уже посчитал сам, и модель дописывает к нему конкретику из разговора, которой
    в карточке не видно («пользователь пришлёт логотип»).
    """
    routes = []
    if card["current"] and card["current"] != task.current:
        task.current = card["current"]
    if card.get("expect"):
        who = card.get("expect_who") or task.expect_who
        if (who, card["expect"]) != (task.expect_who, task.expect):
            task.expect_who, task.expect = who, card["expect"]
            routes.append(Route(layer="working", action="change", kind="expect", source="router",
                                what=f"ждём: {task.expect_line()}"))
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

def rules_from_turn(task: Task, plan: list[str], steps: list,
                    own: frozenset = frozenset()) -> list[Route]:
    """Разложить по рабочей памяти то, что видно из самого обращения.

    Никакой модели здесь нет — это детерминированные правила агента: план стал
    шагами задачи, выполненный инструмент — находкой, записанный файл — артефактом.
    По ним видно, что маршрутизация не сводится к «спросим модель»: часть выбора
    сделана в коде раз и навсегда.

    На паузе правила молчат: отложенная задача не должна обрастать шагами и
    находками из разговора, который идёт уже не про неё.

    `own` — инструменты, которыми агент пишет в самого себя (память, профиль, свод
    правил). Находкой они не становятся: запись правила — это не результат работы
    над задачей, а изменение рамок, и в карточке она была бы шумом. Имена приходят
    снаружи, чтобы модель памяти не знала про соседние сущности.
    """
    if task.paused:
        return []
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
        elif step.tool not in TOOL_NAMES and step.tool not in own:
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
        if kind == "invariant":
            continue     # правило дня 11: его место теперь в своде (app/invariants.py)
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
    payload = {"profile": {}, "decisions": {}, "knowledge": {}}
    bucket = {"profile": "profile", "decision": "decisions", "knowledge": "knowledge"}
    for note in long.notes.values():
        payload[bucket.get(note.kind, "knowledge")][note.key] = note.value
    return payload


def _trim_notes(long: LongTerm) -> None:
    """Удержать долговременную память в пределах потолка: лишнее с конца."""
    if len(long.notes) > config.LONG_LIMIT:
        long.notes = dict(list(long.notes.items())[:config.LONG_LIMIT])


def _parse_flag(value: object) -> bool | None:
    """Булево поле, которое умеет молчать: None — «не упомянуто, ничего не менять»."""
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in ("true", "yes", "да", "1", "on"):
        return True
    if text in ("false", "no", "нет", "0", "off"):
        return False
    return None


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
