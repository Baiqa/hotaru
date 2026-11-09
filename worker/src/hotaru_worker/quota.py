import logging

from google import genai
from google.genai import errors, types

logger = logging.getLogger("hotaru_worker.quota")

# Флаг на процесс (2026-11-09): живой тест Groq-фолбэка показал, что без него
# прогон с полностью мёртвой Gemini-квотой всё равно жжёт полный retry-бюджет
# Gemini (MODEL+FALLBACK_MODEL, до ~150с) на КАЖДОМ claim/событии, прежде чем
# упасть в Groq — на 3000+ claims это заняло бы сутки вместо часов. Один
# модуль-процесс = один run_once(), поэтому глобальный флаг безопасен (не
# переживает рестарт процесса, что и нужно — вдруг квота обновится).
_gemini_dead_this_run = False


def mark_gemini_dead() -> None:
    global _gemini_dead_this_run
    _gemini_dead_this_run = True


def gemini_dead_this_run() -> bool:
    return _gemini_dead_this_run


def is_daily_quota(exc: Exception) -> bool:
    """429 из-за ДНЕВНОГО лимита free-tier (quotaId ...PerDay..., retryDelay в
    часах): повтор через секунды/минуты бессмыслен, ждать нужно до сброса квоты.
    Минутный лимит (PerMinute) сюда не попадает — его retry с backoff лечит.
    Замер 2026-11-09: текст ответа содержит GenerateRequestsPerDayPerProjectPerModel."""
    if not (isinstance(exc, errors.APIError) and exc.code == 429):
        return False
    text = str(exc)
    return "PerDay" in text or "per day" in text.lower()


def has_quota(client: genai.Client, model: str) -> bool:
    """Дешёвая проверка перед батчем. Free-tier — 500 generate_content/день
    (память проекта); без неё каждый claim/событие после исчерпания квоты
    получает полный retry-бюджет (MAX_RETRIES с backoff, затем то же на
    FALLBACK_MODEL), а т.к. run_once() не помечает пропущенный элемент как
    обработанный, внешний while True зацикливается на одном батче бесконечно.
    Один вызов без retry решает "квота есть или нет" до входа в батч-цикл.
    """
    try:
        client.models.generate_content(
            model=model,
            contents="ping",
            config=types.GenerateContentConfig(max_output_tokens=1),
        )
        return True
    except errors.APIError as exc:
        if exc.code == 429:
            logger.warning("дневная квота Gemini похоже исчерпана (смоук-тест вернул 429), пропускаю прогон")
            return False
        raise
