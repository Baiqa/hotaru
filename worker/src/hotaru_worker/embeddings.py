import logging
import time

import httpx
from google import genai
from google.genai import errors, types

logger = logging.getLogger(__name__)

MODEL = "gemini-embedding-2"
OUTPUT_DIMENSIONALITY = 768
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 5

# Таймаут на HTTP-запрос — без него зависший коннект (сервер принял соединение,
# но не ответил) висит бесконечно и не попадает ни в один except ниже (не ошибка,
# а отсутствие ответа). Реальный случай 2026-11-03: процесс завис на 7+ часов с
# нулевой загрузкой CPU, лог не продвинулся ни на строку. Явный таймаут превращает
# зависание в обычный httpx.TransportError, который retry-логика уже умеет обрабатывать.
_HTTP_TIMEOUT_MS = 30_000


def build_client() -> genai.Client:
    return genai.Client(http_options=types.HttpOptions(timeout=_HTTP_TIMEOUT_MS))


def article_text(article: dict) -> str:
    parts = [part for part in (article["title"], article["lead"]) if part]
    return "\n".join(parts)


def embed_text(client: genai.Client, text: str) -> list[float]:
    """Раздел 3.3 CLAUDE.md: "считать по заголовку и лиду, не по всему тексту"."""
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(MAX_RETRIES):
        try:
            result = client.models.embed_content(
                model=MODEL,
                contents=text,
                config=types.EmbedContentConfig(output_dimensionality=OUTPUT_DIMENSIONALITY),
            )
            return result.embeddings[0].values
        except errors.APIError as exc:
            # 429 — либо дневная квота (долгий retryDelay, без смысла ждать 5-80с и
            # лучше сразу упасть и остановить прогон), либо короткий rate limit; 5xx —
            # временная перегрузка free-tier модели (сага 2026-10-26). Оба стоит
            # попробовать переждать короткий backoff, но не больше MAX_RETRIES раз.
            transient = exc.code == 429 or exc.code >= 500
            if not transient or attempt == MAX_RETRIES - 1:
                raise
            logger.warning(
                "Gemini API %s, жду %sс (попытка %s/%s)", exc.code, backoff, attempt + 1, MAX_RETRIES
            )
            time.sleep(backoff)
            backoff *= 2
        except httpx.TransportError as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            logger.warning(
                "сетевой сбой при обращении к Gemini API (%s), жду %sс (попытка %s/%s)",
                exc, backoff, attempt + 1, MAX_RETRIES,
            )
            time.sleep(backoff)
            backoff *= 2
    raise AssertionError("unreachable")
