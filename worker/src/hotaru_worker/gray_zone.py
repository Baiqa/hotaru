import enum
import json
import logging
import os
import time
from pathlib import Path

import httpx
from google import genai
from google.genai import errors, types

from . import groq_client, quota

logger = logging.getLogger(__name__)

# Было временно на preview-модели (2026-09-27: стабильная gemini-3.1-flash-lite держала
# устойчивый 503 "high demand"). 2026-10-20: ситуация обратная — preview держит устойчивый
# 503 почти 2 часа подряд, стабильная отвечает нормально. Вернулись на стабильную; если
# она снова начнёт 503-ить пачками — это известный паттерн у Google, не баг в коде.
MODEL = "gemini-3.1-flash-lite"
# Третий уровень цепочки серой зоны (2026-11-10): gemini-3.5-flash-lite. Сверка на 45
# парах: согласие с кешем основной модели 76%, 0 ложных same из 30, но 7 из 15 same
# уходят в related. Квота у неё, по предположению, отдельная от основной (дневной лимит
# не измерен — выяснится по первому дневному 429). Квота gemini-3.1-flash-lite-preview
# общая с основной, поэтому она НЕ годится как отдельный уровень.
EXTRA_MODEL = "gemini-3.5-flash-lite"
_GROQ_NAME = "groq"
MAX_RETRIES = 8
INITIAL_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 120

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "gray_zone_classify.txt"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")


class DailyQuotaExceeded(Exception):
    """Дневная квота исчерпана у ВСЕХ доступных провайдеров (Gemini, а если
    настроен — и Groq). Повторять в этом прогоне бессмысленно: вызывающий код
    завершает прогон, необработанные статьи подберёт следующий."""


# Groq free-tier кроме дневного лимита имеет минутный: короткую паузу из
# Retry-After пережидаем, длинную считаем дневной квотой.
_GROQ_MAX_WAIT_SECONDS = 60
_GROQ_ATTEMPTS = 30


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

    groq_api_key = os.environ.get("GROQ_API_KEY")
    last_exc: Exception | None = None

    # Уровень 1: основная Gemini (пропускаем, если уже признана мёртвой в этом прогоне —
    # не жжём на ней retry-бюджет).
    if not quota.gemini_dead_this_run():
        try:
            return _classify_gemini(client, prompt, MODEL)
        except (errors.APIError, httpx.TransportError) as exc:
            if quota.is_daily_quota(exc):
                quota.mark_gemini_dead()
            logger.warning("Gemini недоступна для серой зоны (%s), иду по цепочке дальше", type(exc).__name__)
            last_exc = exc

    # Уровень 2: Groq (если настроен).
    if groq_api_key and not quota.is_dead(_GROQ_NAME):
        try:
            return _classify_groq(groq_api_key, prompt)
        except DailyQuotaExceeded as exc:
            quota.mark_dead(_GROQ_NAME)
            logger.warning("Groq: дневной лимит исчерпан, иду по цепочке дальше")
            last_exc = exc
        except httpx.HTTPError as exc:  # HTTPStatusError (не-429 / исчерпанные повторы), TransportError
            logger.warning("Groq недоступен для серой зоны (%s), иду по цепочке дальше", type(exc).__name__)
            last_exc = exc

    # Уровень 3: вторая Gemini-модель с отдельной (предположительно) квотой.
    if not quota.is_dead(EXTRA_MODEL):
        try:
            return _classify_gemini(client, prompt, EXTRA_MODEL)
        except (errors.APIError, httpx.TransportError) as exc:
            if quota.is_daily_quota(exc):
                quota.mark_dead(EXTRA_MODEL)
                logger.warning("%s: дневная квота исчерпана", EXTRA_MODEL)
            last_exc = exc

    all_daily = (
        quota.gemini_dead_this_run()
        and (not groq_api_key or quota.is_dead(_GROQ_NAME))
        and quota.is_dead(EXTRA_MODEL)
    )
    if all_daily:
        raise DailyQuotaExceeded("дневные квоты всех провайдеров серой зоны исчерпаны") from last_exc
    assert last_exc is not None
    raise last_exc


def _classify_gemini(client: genai.Client, prompt: str, model: str) -> Verdict:
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(MAX_RETRIES):
        try:
            result = client.models.generate_content(
                model=model,
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
            if quota.is_daily_quota(exc):
                raise
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
    raise AssertionError("unreachable")  # цикл либо вернул, либо поднял исключение


def _classify_groq(api_key: str, prompt: str) -> Verdict:
    """Третий уровень (2026-11-09). Промпт просит «одно слово» под Gemini
    enum-режим; у Groq схемной привязки нет, поэтому добавляем JSON-инструкцию."""
    groq_prompt = prompt + (
        '\n\nОтветь в формате JSON: {"verdict": "same"} (или "related", или "different").'
    )
    for attempt in range(_GROQ_ATTEMPTS):
        try:
            content = groq_client.call_json(api_key, groq_prompt)
            break
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429:
                raise
            try:
                wait = float(exc.response.headers.get("retry-after", "inf"))
            except ValueError:
                wait = float("inf")
            if wait > _GROQ_MAX_WAIT_SECONDS:
                # Длинный Retry-After — дневной лимит. Короткий — минутный: при
                # непрерывной нагрузке он срабатывает подряд (живой прогон
                # 2026-11-09: три 429 подряд ошибочно остановили всю кластеризацию),
                # поэтому ждём и повторяем, а после _GROQ_ATTEMPTS — отдаём ошибку
                # вызывающему коду (он пропустит статью, но не остановит прогон).
                raise DailyQuotaExceeded("лимит Groq исчерпан (Retry-After=%s)" % wait) from exc
            if attempt == _GROQ_ATTEMPTS - 1:
                raise
            logger.warning("Groq 429, жду %sс (попытка %s/%s)", wait, attempt + 1, _GROQ_ATTEMPTS)
            time.sleep(wait)
    try:
        return Verdict(str(json.loads(content)["verdict"]).strip())
    except (ValueError, KeyError, TypeError):
        logger.warning("нераспознанный ответ Groq серой зоны: %r, считаю different", content)
        return Verdict.DIFFERENT
