import enum
import json
import logging
import os
import time
from pathlib import Path

import httpx
from google import genai
from google.genai import errors, types

from . import db, groq_client, quota

logger = logging.getLogger(__name__)

# Короткий retry-бюджет с фолбэком на вторую модель — тот же урок, что и в
# claim_extraction.py (см. там и память проекта про 503-сагу Gemini, 2026-10-26).
MODEL = "gemini-3.1-flash-lite"
FALLBACK_MODEL = "gemini-3.1-flash-lite-preview"
MAX_RETRIES = 4
INITIAL_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 60

# Пороги — стартовые, без размеченных данных (как и пороги событий на старте Этапа 1),
# калибровка откладывается на потом. CANDIDATE_TRIES — защита от той же ошибки, что
# калибровали в Этапе 1 (сравнение только с top-1 кандидатом хрупко).
HIGH_THRESHOLD = 0.80
LOW_THRESHOLD = 0.55
CANDIDATE_TRIES = 3

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "claim_relation_classify.txt"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")


class Relation(enum.Enum):
    CONFIRMS = "confirms"
    CONTRADICTS = "contradicts"
    DIFFERENT = "different"


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _call_model(client: genai.Client, model: str, prompt: str) -> Relation:
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(MAX_RETRIES):
        try:
            result = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="text/x.enum",
                    response_schema=Relation,
                ),
            )
            try:
                return Relation(result.text.strip())
            except ValueError:
                logger.warning("нераспознанный ответ LLM классификации claims: %r, считаю different", result.text)
                return Relation.DIFFERENT
        except errors.APIError as exc:
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
    raise AssertionError("unreachable")


def _classify_relation_groq(api_key: str, prompt: str) -> Relation:
    """Третий уровень fallback (2026-11-08) — после обеих моделей Gemini.
    claim_relation_classify.txt просит "одним словом" под Gemini enum-режим
    (response_schema=Relation); у Groq такой схемной привязки нет, поэтому
    здесь — отдельная JSON-инструкция поверх общего промпта, не трогая сам
    файл (он и дальше работает для Gemini как есть)."""
    groq_prompt = prompt + (
        '\n\nОтветь в формате JSON: {"relation": "confirms"} '
        '(или "contradicts", или "different").'
    )
    content = groq_client.call_json(api_key, groq_prompt)
    return Relation(json.loads(content)["relation"])


def _classify_relation(client: genai.Client, candidate: dict, new_claim: dict) -> Relation:
    prompt = _PROMPT_TEMPLATE.format(
        text_a=candidate["text"], evidence_a=candidate["evidence"],
        text_b=new_claim["text"], evidence_b=new_claim["evidence"],
    )
    groq_api_key = os.environ.get("GROQ_API_KEY")

    # Живой тест 2026-11-09: без этой проверки прогон с мёртвой Gemini-квотой
    # жжёт полный retry-бюджет Gemini (до ~150с) НА КАЖДОМ claim, прежде чем
    # упасть в Groq — на 3000+ claims это заняло бы сутки. Если уже знаем, что
    # Gemini мёртв в этом прогоне (смоук-тест или более ранний 429), сразу идём
    # в Groq, не повторяя обречённые попытки.
    if groq_api_key and quota.gemini_dead_this_run():
        return _classify_relation_groq(groq_api_key, prompt)

    try:
        return _call_model(client, MODEL, prompt)
    except (errors.APIError, httpx.TransportError):
        logger.warning("основная модель %s не отвечает, пробую фолбэк %s", MODEL, FALLBACK_MODEL)
        try:
            return _call_model(client, FALLBACK_MODEL, prompt)
        except (errors.APIError, httpx.TransportError) as exc2:
            if isinstance(exc2, errors.APIError) and exc2.code == 429:
                quota.mark_gemini_dead()
            if not groq_api_key:
                raise
            logger.warning("обе модели Gemini недоступны, пробую Groq (%s)", groq_client.MODEL)
            return _classify_relation_groq(groq_api_key, prompt)


def group_claim(conn, client: genai.Client, claim: dict, event_id: str) -> str:
    """Раздел 3.4 CLAUDE.md: присоединяет claim к существующей группе события события
    (confirms/contradicts) либо заводит новую. claim — dict с id/text/evidence/embedding
    (embedding как pgvector.Vector или list[float], приводим через .to_list() при чтении
    из БД — вызывающий код должен передавать list[float]).
    Возвращает "confirms" | "contradicts" | "new" — для статистики раннера.
    """
    groups = db.event_claim_groups(conn, event_id)
    claim_embedding = claim["embedding"].to_list() if hasattr(claim["embedding"], "to_list") else claim["embedding"]

    scored = []
    for group in groups:
        if group["centroid"] is None:
            continue
        centroid = group["centroid"].to_list() if hasattr(group["centroid"], "to_list") else group["centroid"]
        similarity = _cosine_similarity(claim_embedding, centroid)
        scored.append((similarity, group))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    for similarity, group in scored[:CANDIDATE_TRIES]:
        if similarity < LOW_THRESHOLD:
            continue

        numbers_conflict = db.claim_group_numbers_conflict(conn, group["id"], claim["id"])

        if similarity >= HIGH_THRESHOLD and not numbers_conflict:
            relation = Relation.CONFIRMS
        else:
            representative = db.claim_group_representative(conn, group["id"])
            relation = _classify_relation(client, representative, claim)

        if relation is Relation.DIFFERENT:
            continue

        db.add_claim_group_member(conn, group["id"], claim["id"], relation.value, similarity)
        db.recompute_claim_group(conn, group["id"])
        return relation.value

    db.create_claim_group(conn, event_id, claim["id"], claim["embedding"])
    return "new"
