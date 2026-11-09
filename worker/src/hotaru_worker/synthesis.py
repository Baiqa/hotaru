import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from . import groq_client, quota

logger = logging.getLogger(__name__)

# Раздел 8 CLAUDE.md, открытый вопрос: "сильная модель на синтезе" — на практике
# 2026-11-04 выяснилось, что любая pro-модель недоступна без платного тарифа
# (gemini-2.5-pro: 404 "no longer available to new users"; gemini-3.1-pro: 429
# с limit=0 на free tier — не дневная квота, лимит буквально нулевой). Решение:
# пока работаем на той же дешёвой модели, что и extract_claims/group_claims —
# это даёт MVP-качество текста, а не затыкает весь Этап 2.4. Переход на pro —
# одна строка здесь, когда подключим платный тариф (см. память проекта).
MODEL = "gemini-3.1-flash-lite"
FALLBACK_MODEL = "gemini-3.1-flash-lite-preview"
MAX_RETRIES = 4
INITIAL_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 60

# Раздел 3.4 CLAUDE.md: "ни одно предложение не совпадает дословно с оригиналом
# длиннее 8 слов" — порог окна шингла.
SHINGLE_WINDOW = 8
# Генерация целиком отклоняется и повторяется, если число не найдено среди фактов
# (раздел 3.4, правило 2) — но не больше этого числа попыток, иначе событие просто
# пропускается в этом прогоне (не блокирует остальные события).
MAX_GENERATION_RETRIES = 2

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
_PROMPT_TEMPLATES = {
    "summary_30s": (_PROMPTS_DIR / "synthesize_summary_30s.txt").read_text(encoding="utf-8"),
    "full_breakdown": (_PROMPTS_DIR / "synthesize_full_breakdown.txt").read_text(encoding="utf-8"),
}

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_WORD_RE = re.compile(r"\w+", re.UNICODE)


class _Sentence(BaseModel):
    text: str
    claim_ids: list[str]


class _SynthesisResponse(BaseModel):
    sentences: list[_Sentence]


def build_client() -> genai.Client:
    # См. embeddings.py: без явного таймаута зависший запрос к Gemini не попадает
    # ни в один except и висит часами с нулевой загрузкой CPU (случай 2026-11-03).
    return genai.Client(http_options=types.HttpOptions(timeout=60_000))


def _format_number(value: float) -> str:
    # Без этого модель эхом повторяет "165.0 миль в час" вместо "165 миль в час" —
    # она буквально копирует то, что видит в фактах (и это даже правильно по
    # правилу 2 раздела 3.4: число в тексте должно совпадать с числом в факте).
    return str(int(value)) if value == int(value) else f"{value:g}"


def _format_facts(facts: list[dict]) -> str:
    lines = []
    for fact in facts:
        numbers = ", ".join(
            f"{n['label']}={_format_number(n['value'])}{n['unit'] or ''} ({n['period']})"
            if n["period"]
            else f"{n['label']}={_format_number(n['value'])}{n['unit'] or ''}"
            for n in fact["numbers"]
        )
        lines.append(
            f"- claim_id={fact['claim_id']} bucket={fact['bucket']} источник={fact['source_name']}\n"
            f"  текст: {fact['text']}\n"
            f"  numbers: [{numbers}]"
        )
    return "\n".join(lines)


def _parse_synthesis_json(raw: str) -> _SynthesisResponse:
    """Общий парсер для Gemini и Groq. Промпты синтеза просят JSON-список
    предложений буквально (нужно Groq'у, чтобы response_format=json_object не
    упал с 400 — OpenAI-совместимые API требуют слово "json" в промпте), но
    наша схема — объект {"sentences": [...]}; у Gemini это не противоречие
    (response_schema навязывает форму независимо от буквального текста
    промпта), а для Groq, у которого схемной привязки нет, нужна терпимость
    к обеим формам."""
    data = json.loads(raw)
    if isinstance(data, list):
        data = {"sentences": data}
    return _SynthesisResponse.model_validate(data)


def _call_model_groq(api_key: str, prompt: str) -> _SynthesisResponse:
    content = groq_client.call_json(api_key, prompt)
    return _parse_synthesis_json(content)


def _call_model_once(client: genai.Client, model: str, prompt: str) -> _SynthesisResponse:
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(MAX_RETRIES):
        try:
            result = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_SynthesisResponse,
                ),
            )
            return _parse_synthesis_json(result.text)
        except errors.APIError as exc:
            if quota.is_daily_quota(exc):
                raise
            transient = exc.code == 429 or exc.code >= 500
            if not transient or attempt == MAX_RETRIES - 1:
                raise
            logger.warning("Gemini API %s (%s), жду %sс (попытка %s/%s)", exc.code, model, backoff, attempt + 1, MAX_RETRIES)
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


def _call_model(client: genai.Client, prompt: str) -> _SynthesisResponse:
    """Тот же урок, что в claim_grouping.py: короткий retry-бюджет на основной
    модели, затем фолбэк на вторую — сага 503 2026-10-26."""
    groq_api_key = os.environ.get("GROQ_API_KEY")

    # Живой тест 2026-11-09: без этой проверки прогон с мёртвой Gemini-квотой
    # жжёт полный retry-бюджет Gemini на КАЖДОМ событии/формате, прежде чем
    # упасть в Groq. Если уже знаем, что Gemini мёртв в этом прогоне, сразу
    # идём в Groq.
    if groq_api_key and quota.gemini_dead_this_run():
        return _call_model_groq(groq_api_key, prompt)

    try:
        return _call_model_once(client, MODEL, prompt)
    except (errors.APIError, httpx.TransportError):
        logger.warning("основная модель %s не отвечает, пробую фолбэк %s", MODEL, FALLBACK_MODEL)
        try:
            return _call_model_once(client, FALLBACK_MODEL, prompt)
        except (errors.APIError, httpx.TransportError) as exc2:
            if isinstance(exc2, errors.APIError) and exc2.code == 429:
                quota.mark_gemini_dead()
            if not groq_api_key:
                raise
            logger.warning("обе модели Gemini недоступны, пробую Groq (%s)", groq_client.MODEL)
            return _call_model_groq(groq_api_key, prompt)


def _word_shingles(text: str, n: int) -> set[tuple[str, ...]]:
    words = tuple(w.lower() for w in _WORD_RE.findall(text))
    if len(words) < n:
        return set()
    return {words[i : i + n] for i in range(len(words) - n + 1)}


def _copies_original(sentence_text: str, evidence_shingles: set[tuple[str, ...]]) -> bool:
    if not evidence_shingles:
        return False
    return bool(_word_shingles(sentence_text, SHINGLE_WINDOW) & evidence_shingles)


def _extract_numbers(text: str) -> set[float]:
    found = set()
    for match in _NUMBER_RE.findall(text):
        try:
            found.add(round(float(match.replace(",", ".")), 2))
        except ValueError:
            continue
    return found


def _is_year_like(value: float) -> bool:
    return value == int(value) and 1900 <= value <= 2100


def generate_render(client: genai.Client, event_id: str, facts: list[dict], format: str) -> tuple[str, list[list[str]]] | None:
    """Раздел 3.4 CLAUDE.md: модель видит ТОЛЬКО facts (не исходные статьи).
    Возвращает (body, sentence_claims) либо None, если даже после
    MAX_GENERATION_RETRIES не удалось получить проверяемую генерацию — вызывающий
    код должен пропустить событие в этом прогоне, не блокируя остальные.
    """
    # claim_id из БД приходит как uuid.UUID, модель возвращает строки — сравниваем
    # как строки, иначе `str in set[UUID]` всегда False (реальный баг, 2026-11-04).
    allowed_claim_ids = {str(fact["claim_id"]) for fact in facts}
    allowed_numbers = {round(n["value"], 2) for fact in facts for n in fact["numbers"]}
    evidence_shingles: set[tuple[str, ...]] = set()
    for fact in facts:
        evidence_shingles |= _word_shingles(fact["evidence"], SHINGLE_WINDOW)

    prompt_template = _PROMPT_TEMPLATES[format]
    prompt = prompt_template.format(facts=_format_facts(facts))

    for attempt in range(MAX_GENERATION_RETRIES):
        response = _call_model(client, prompt)

        kept_sentences: list[str] = []
        kept_claim_ids: list[list[str]] = []
        for sentence in response.sentences:
            cited = [cid for cid in sentence.claim_ids if cid in allowed_claim_ids]
            if not cited:
                logger.info("событие %s: предложение без claim_id отброшено: %r", event_id, sentence.text)
                continue
            if _copies_original(sentence.text, evidence_shingles):
                logger.info("событие %s: предложение совпадает с оригиналом, отброшено: %r", event_id, sentence.text)
                continue
            kept_sentences.append(sentence.text)
            kept_claim_ids.append(cited)

        if not kept_sentences:
            logger.warning("событие %s: после пост-проверки не осталось ни одного предложения, пропускаю", event_id)
            return None

        bad_numbers = {
            n
            for text in kept_sentences
            for n in _extract_numbers(text)
            if not _is_year_like(n) and n not in allowed_numbers
        }
        if bad_numbers:
            logger.warning(
                "событие %s: числа %s не найдены среди фактов (попытка %s/%s), повторяю генерацию",
                event_id, bad_numbers, attempt + 1, MAX_GENERATION_RETRIES,
            )
            continue

        return "\n".join(kept_sentences), kept_claim_ids

    logger.warning("событие %s: генерация не прошла пост-проверку по числам после %s попыток, пропускаю", event_id, MAX_GENERATION_RETRIES)
    return None
