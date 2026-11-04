import logging

from google import genai
from google.genai import errors, types

logger = logging.getLogger("hotaru_worker.quota")


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
