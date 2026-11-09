import logging
import time
from pathlib import Path
from typing import Literal

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from . import quota

logger = logging.getLogger(__name__)

# Та же модель, что в gray_zone.py — дешёвая, retry/backoff уже отлажен под неё
# на реальном трафике (см. gray_zone.py для истории про 503, сейчас на стабильной версии).
MODEL = "gemini-3.1-flash-lite"
# Вторая модель как фолбэк (см. 2026-10-26: 503 "high demand" у Google бьёт по моделям
# не синхронно — 27 сентября лежала preview-версия при живой стабильной, 21 октября
# наоборот. Один доп. заход на другую модель — дешёвый шанс спасти статью).
FALLBACK_MODEL = "gemini-3.1-flash-lite-preview"
# Короче, чем раньше (было 8/120, до ~8.6 мин на одну зависшую статью): при затяжном сбое
# Google это просто съедало часы впустую, а повторная попытка всё равно случится на
# следующем перезапуске благодаря claims_extracted_at-кешу. Пусть статья сдаётся быстрее.
MAX_RETRIES = 4
INITIAL_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 60

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "extract_claims.txt"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")


class ClaimNumber(BaseModel):
    value: float
    unit: str | None = None
    label: str | None = None
    period: str | None = None


class Claim(BaseModel):
    text: str
    type: Literal["fact", "statement", "estimate"]
    evidence: str
    numbers: list[ClaimNumber] = []


class ClaimsResponse(BaseModel):
    claims: list[Claim]


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _verify_evidence(article_text: str, evidence: str) -> bool:
    """Раздел 3.2 CLAUDE.md, жёсткое правило: evidence должен буквально находиться в
    исходном тексте — иначе claim отбрасывается. Нормализуем только пробелы/переносы
    строк (модели регулярно ломают их при цитировании), не регистр и не пунктуацию —
    это всё ещё точная подстрока, не fuzzy-совпадение."""
    if not evidence:
        return False
    return _normalize_whitespace(evidence) in _normalize_whitespace(article_text)


def _call_model(client: genai.Client, model: str, prompt: str) -> ClaimsResponse | None:
    """Retry/backoff на одной модели. Возвращает None, если модель вернула
    нераспознанный JSON (не ошибка — просто считаем 0 claims). Поднимает последнее
    исключение, если все попытки исчерпаны."""
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(MAX_RETRIES):
        try:
            result = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ClaimsResponse,
                ),
            )
            return result.parsed
        except errors.APIError as exc:
            if quota.is_daily_quota(exc):
                raise
            transient = exc.code == 429 or exc.code >= 500
            if not transient or attempt == MAX_RETRIES - 1:
                raise
            logger.warning(
                "Gemini API %s (%s), жду %sс (попытка %s/%s)", exc.code, model, backoff, attempt + 1, MAX_RETRIES
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        except httpx.TransportError as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            logger.warning(
                "сетевой сбой при обращении к Gemini API (%s, %s), жду %sс (попытка %s/%s)",
                model, exc, backoff, attempt + 1, MAX_RETRIES,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    raise AssertionError("unreachable")  # цикл либо вернул, либо поднял исключение


def extract_claims(client: genai.Client, article: dict) -> list[dict]:
    """Вызывает LLM на статье, отбрасывает claims без подтверждённого evidence.
    Возвращает список dict с ключами text/type/evidence/numbers — то, что кладётся в БД.

    Если основная модель исчерпала retry (устойчивый 503 и т.п.) — один доп. заход на
    FALLBACK_MODEL, прежде чем сдаться и дать вызывающему коду пропустить статью.
    """
    full_text = "\n".join(
        part for part in (article.get("title"), article.get("lead"), article.get("body")) if part
    )
    prompt = _PROMPT_TEMPLATE.format(
        title=article.get("title") or "",
        lead=article.get("lead") or "",
        body=article.get("body") or "",
    )

    try:
        parsed = _call_model(client, MODEL, prompt)
    except (errors.APIError, httpx.TransportError):
        logger.warning("основная модель %s не отвечает, пробую фолбэк %s", MODEL, FALLBACK_MODEL)
        parsed = _call_model(client, FALLBACK_MODEL, prompt)

    if parsed is None:
        logger.warning("нераспознанный JSON-ответ LLM при извлечении фактов, считаю 0 claims")
        return []

    verified = []
    for claim in parsed.claims:
        if not _verify_evidence(full_text, claim.evidence):
            logger.info("claim отброшен — evidence не найден в тексте: %r", claim.evidence[:80])
            continue
        verified.append(
            {
                "text": claim.text,
                "type": claim.type,
                "evidence": claim.evidence,
                "numbers": [n.model_dump() for n in claim.numbers],
            }
        )
    return verified
