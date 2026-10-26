import enum
import logging
import time
from pathlib import Path

import httpx
from google import genai
from google.genai import errors, types

logger = logging.getLogger(__name__)

# Было временно на preview-модели (2026-09-27: стабильная gemini-3.1-flash-lite держала
# устойчивый 503 "high demand"). 2026-10-20: ситуация обратная — preview держит устойчивый
# 503 почти 2 часа подряд, стабильная отвечает нормально. Вернулись на стабильную; если
# она снова начнёт 503-ить пачками — это известный паттерн у Google, не баг в коде.
MODEL = "gemini-3.1-flash-lite"
MAX_RETRIES = 8
INITIAL_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 120

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "gray_zone_classify.txt"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")


class Verdict(enum.Enum):
    SAME = "same"
    RELATED = "related"
    DIFFERENT = "different"


def classify(client: genai.Client, article_a: dict, article_b: dict) -> Verdict:
    """Раздел 3.3 CLAUDE.md: серая зона — LLM получает два заголовка+лида, отвечает
    строго same|related|different. Промпт — в prompts/gray_zone_classify.txt (раздел 7
    CLAUDE.md требует промпты отдельными файлами, не в коде).

    При сбое/непарсируемом ответе — безопасный дефолт `different`: лучше не склеить
    два разных события, чем ошибочно смешать в одном факты о разных.
    """
    prompt = _PROMPT_TEMPLATE.format(
        title_a=article_a.get("title") or "",
        lead_a=article_a.get("lead") or "",
        title_b=article_b.get("title") or "",
        lead_b=article_b.get("lead") or "",
    )

    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(MAX_RETRIES):
        try:
            result = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="text/x.enum",
                    response_schema=Verdict,
                ),
            )
            try:
                return Verdict(result.text.strip())
            except ValueError:
                logger.warning("нераспознанный ответ LLM серой зоны: %r, считаю different", result.text)
                return Verdict.DIFFERENT
        except errors.APIError as exc:
            transient = exc.code == 429 or exc.code >= 500
            if not transient or attempt == MAX_RETRIES - 1:
                raise
            logger.warning(
                "Gemini API %s, жду %sс (попытка %s/%s)", exc.code, backoff, attempt + 1, MAX_RETRIES
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        except httpx.TransportError as exc:
            # Обрыв соединения на уровне сети (не структурированная ошибка API) —
            # реальный случай: httpx.ReadError, [WinError 10053], на живом прогоне.
            if attempt == MAX_RETRIES - 1:
                raise
            logger.warning(
                "сетевой сбой при обращении к Gemini API (%s), жду %sс (попытка %s/%s)",
                exc, backoff, attempt + 1, MAX_RETRIES,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
