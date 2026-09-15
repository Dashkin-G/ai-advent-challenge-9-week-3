"""Транспорт до LLM: единственное место, которое знает про HTTP API модели.

Здесь нет ни памяти, ни ролей, ни правил диалога — только «отправить сообщения,
получить ответ и метрики». Всё поведение живёт в агенте (app/agent.py).

Клиент единый (keep-alive): эндпоинт в Сингапуре, повторный TCP/TLS дорог.
"""
import logging
import time

from openai import OpenAI

from . import config

logger = logging.getLogger("app.llm")

_LOG_SEP = "─" * 60


def _oneline(text: str, limit: int = 200) -> str:
    """Однострочная выжимка содержимого для лога (без каши из переносов)."""
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[:limit] + f"… ({len(text)} симв.)"
    return text


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not config.DASHSCOPE_API_KEY:
            raise RuntimeError("DASHSCOPE_API_KEY не задан. Скопируйте .env.example в .env и укажите ключ.")
        _client = OpenAI(
            api_key=config.DASHSCOPE_API_KEY,
            base_url=config.DASHSCOPE_BASE_URL,
        )
    return _client


def chat(
    messages: list[dict],
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
) -> dict:
    """Отправить готовые сообщения в модель и вернуть ответ с метриками.

    temperature / max_tokens / tools передаём, только если заданы, иначе действуют
    дефолты провайдера. Внимание: temperature=0 — валидное значение, поэтому
    проверяем `is not None`, а не truthiness (иначе ноль потеряется).

    Если переданы `tools` (описания инструментов), модель может вместо ответа
    попросить их вызвать: тогда `finish_reason` будет `tool_calls`, а сами вызовы
    вернутся в `tool_calls`. Исполняет их агент — транспорт только передаёт.

    Кроме текста возвращаем точный слепок запроса (`request`) и сырой ответ модели
    (`response`) — интерфейс показывает их в разделе «сырой обмен».
    """
    model = model or config.DEFAULT_MODEL

    params: dict = {}
    if temperature is not None:
        params["temperature"] = temperature
    if max_tokens:
        params["max_tokens"] = max_tokens
    if tools:
        params["tools"] = tools

    # Тело запроса ровно в том виде, как уходит по сети (enable_thinking — через
    # extra_body; SDK вкладывает его в тот же JSON).
    request_body = {"model": model, "messages": messages, **params, "enable_thinking": False}

    msgs = "\n".join(
        f"│ {m.get('role', '?'):<9} {_oneline(m.get('content', ''))}" for m in messages
    )
    logger.info(
        "┌─ LLM → запрос · model=%s · t°=%s · max_tokens=%s\n%s\n└%s",
        model, temperature, max_tokens, msgs, _LOG_SEP,
    )

    # enable_thinking=False -> нестриминговый ответ (минимум латентности и кода).
    t0 = time.perf_counter()
    resp = _get_client().chat.completions.create(
        model=model,
        messages=messages,
        extra_body={"enable_thinking": False},
        **params,
    )
    elapsed = time.perf_counter() - t0

    choice = resp.choices[0]
    content = choice.message.content or ""  # финальный ответ берём из content, не из reasoning

    # Запросы инструментов: отдаём агенту разобранными и собираем сообщение
    # ассистента в чистом виде — его агент положит обратно в диалог перед
    # результатами инструментов (этого требует протокол tool-вызовов).
    tool_calls = [
        {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
        for tc in (choice.message.tool_calls or [])
    ]
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in tool_calls
        ]

    usage = None
    if resp.usage is not None:
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens,
        }

    tokens = (
        f"{usage['prompt_tokens']}→{usage['completion_tokens']} (всего {usage['total_tokens']})"
        if usage else "n/a"
    )
    shown = content if len(content) <= 1000 else content[:1000] + f"…\n[обрезано, всего {len(content)} симв.]"
    body = "\n".join("│ " + ln for ln in (shown.splitlines() or [""]))
    logger.info(
        "┌─ LLM ← ответ · finish=%s · %.2fs · tokens=%s\n%s\n└%s",
        choice.finish_reason, elapsed, tokens, body, _LOG_SEP,
    )

    cost = (
        config.cost_usd(model, usage["prompt_tokens"], usage["completion_tokens"])
        if usage else None
    )

    return {
        "content": content,
        "message": message,                     # сообщение ассистента для продолжения диалога
        "tool_calls": tool_calls,               # что модель просит выполнить (может быть пусто)
        "model": model,
        "finish_reason": choice.finish_reason,  # "stop" | "length" — чем закончилась генерация
        "usage": usage,
        "temperature": temperature,             # эхо: какая температура фактически ушла
        "elapsed_s": round(elapsed, 3),         # время полного round-trip, сек
        "cost_usd": cost,                       # теоретическая стоимость (USD) или None
        "tier": config.model_tier(model),       # ресурсоёмкость (лёгкая/средняя/тяжёлая)
        "request": request_body,                # «сырой обмен» → запрос
        "response": resp.model_dump(mode="json"),  # «сырой обмен» ← ответ (как есть)
    }
