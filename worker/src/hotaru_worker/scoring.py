import math
import re
import time
from datetime import timedelta

from . import db

# Раздел 3.3 CLAUDE.md — веса сигналов близости на уровне событий.
WEIGHT_ENTITY_JACCARD = 0.35
WEIGHT_TOKENS_JACCARD = 0.20
WEIGHT_COSINE = 0.30
WEIGHT_TIME_DECAY = 0.15

TIME_DECAY_HALF_LIFE_HOURS = 24

# Событие — окно "часы-дни" (в отличие от сюжета, окно которого "недели-месяцы"),
# 5 дней — разумный дефолт с запасом на выходные; веса и окно уточняются на
# размеченной выборке, когда появятся events (раздел 3.3 CLAUDE.md).
DEFAULT_WINDOW_DAYS = 5
DEFAULT_CANDIDATE_LIMIT = 50

# Жёсткий лимит события (CLAUDE.md §3.3, «Дрейф»: «жёсткий лимит на длину окна события»):
# событие — один факт реальности, а не цепочка. Без лимита статьи по одной приклеивались
# к краю события, и оно разрасталось (проверено: «Белгород, 79 статей»). Дальше цепочку
# связывают сюжеты (stories), а не событие. Применяется при присоединении статьи
# (clustering.py) и при слиянии событий (event_merge.py).
MAX_EVENT_SPAN = timedelta(hours=72)
MAX_EVENT_ARTICLES = 40


def within_event_limits(count_a: int, first_a, last_a, count_b: int, first_b, last_b) -> bool:
    """Уложится ли объединение двух групп статей (статья = группа из одной, first == last)
    в лимит события. first/last — время публикации; None — неизвестно, не считается."""
    if count_a + count_b > MAX_EVENT_ARTICLES:
        return False
    firsts = [t for t in (first_a, first_b) if t is not None]
    lasts = [t for t in (last_a, last_b) if t is not None]
    if firsts and lasts and max(lasts) - min(firsts) > MAX_EVENT_SPAN:
        return False
    return True

_NUMBER_RE = re.compile(r"\d[\d\s]*(?:[.,]\d+)?%?")

_MONTHS = {
    "январ": "01", "феврал": "02", "март": "03", "апрел": "04", "мая": "05", "июн": "06",
    "июл": "07", "август": "08", "сентябр": "09", "октябр": "10", "ноябр": "11", "декабр": "12",
    "january": "01", "february": "02", "march": "03", "april": "04", "may": "05", "june": "06",
    "july": "07", "august": "08", "september": "09", "october": "10", "november": "11", "december": "12",
}
_MONTH_NAME_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\w*|\b(" + "|".join(_MONTHS) + r")\w*\s+(\d{1,2})\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DOTTED_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")


def _normalize_number(raw: str) -> str | None:
    has_percent = raw.endswith("%")
    digits = raw.rstrip("%").replace(" ", "").replace(",", ".")
    if not digits:
        return None
    # Отсекаем короткие "голые" числа (1-2 цифры без дробной части и без "%") —
    # слишком частый шум (нумерация списков и т.п.), не полезный сигнал совпадения.
    integer_part = digits.split(".")[0]
    if len(integer_part) < 3 and "." not in digits and not has_percent:
        return None
    return digits + ("%" if has_percent else "")


def extract_number_date_tokens(text: str) -> set[str]:
    """Раздел 3.3 CLAUDE.md: "совпадение чисел и дат" — сильный и дешёвый сигнал.
    Дешёвый = regex по тексту, а не полноценное извлечение фактов (это Этап 2)."""
    tokens: set[str] = set()

    for match in _NUMBER_RE.finditer(text):
        normalized = _normalize_number(match.group())
        if normalized:
            tokens.add(f"num:{normalized}")

    for match in _ISO_DATE_RE.finditer(text):
        tokens.add(f"date:{match.group(1)}-{match.group(2)}-{match.group(3)}")

    for match in _DOTTED_DATE_RE.finditer(text):
        day, month, year = match.groups()
        tokens.add(f"date:{year}-{int(month):02d}-{int(day):02d}")

    for match in _MONTH_NAME_RE.finditer(text):
        day, month_name = (match.group(1), match.group(2)) if match.group(1) else (match.group(4), match.group(3))
        month_key = next(key for key in _MONTHS if month_name.lower().startswith(key))
        tokens.add(f"monthday:{_MONTHS[month_key]}-{int(day):02d}")

    return tokens


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def entity_jaccard(entities_a: set[str], entities_b: set[str]) -> float:
    return _jaccard(entities_a, entities_b)


# --- IDF-веса сущностей (фаза 4 плана P0) -----------------------------------------
# Обычный Жаккар считает «Россию» (30% потока) и редкого фигуранта одинаково. Из-за этого
# у крупных источников общая частая сущность не отсекала кандидатов, а верная пара
# с редкой общей сущностью тонула (находка из tmp/plans/etap1-kalibrovka-po-keisam.md:
# «верная пара на ранге #17–19, задавлена частыми сущностями США/МИД/Россия»).
# Вес сущности = ln((N+1)/(df+1)) + 1: у встреченной везде — 1, у редкой — до ~ln(N)+1.
# Единица в конце нужна, чтобы самая частая сущность не обнулялась совсем.

IDF_TTL_SECONDS = 600


class EntityWeights:
    def __init__(self, total_articles: int, doc_freq: dict[str, int]):
        self.total_articles = total_articles
        self.doc_freq = doc_freq

    def weight(self, entity_id: str) -> float:
        return math.log((self.total_articles + 1) / (self.doc_freq.get(entity_id, 0) + 1)) + 1.0


_weights_cache: tuple[float, EntityWeights] | None = None


def get_entity_weights(conn) -> EntityWeights:
    """Частоты — одним запросом на прогон; обновляются раз в IDF_TTL_SECONDS, чтобы
    долгий прогон кластеризации не жил на устаревших числах."""
    global _weights_cache
    now = time.monotonic()
    if _weights_cache is None or now - _weights_cache[0] > IDF_TTL_SECONDS:
        total, doc_freq = db.entity_document_frequencies(conn)
        _weights_cache = (now, EntityWeights(total, doc_freq))
    return _weights_cache[1]


def weighted_entity_jaccard(entities_a: set[str], entities_b: set[str], weights: EntityWeights) -> float:
    union = entities_a | entities_b
    if not union:
        return 0.0
    total = sum(weights.weight(e) for e in union)
    common = sum(weights.weight(e) for e in entities_a & entities_b)
    return common / total if total else 0.0


def tokens_jaccard(tokens_a: set[str], tokens_b: set[str]) -> float:
    return _jaccard(tokens_a, tokens_b)


def time_decay(published_a, published_b) -> float:
    if published_a is None or published_b is None:
        return 0.0
    delta_hours = abs((published_a - published_b).total_seconds()) / 3600
    return math.exp(-math.log(2) * delta_hours / TIME_DECAY_HALF_LIFE_HOURS)


def _full_text(article: dict) -> str:
    return "\n".join(part for part in (article.get("title"), article.get("lead"), article.get("body")) if part)


def score_pair(
    article_a: dict,
    entities_a: set[str],
    article_b: dict,
    entities_b: set[str],
    cosine_sim: float,
    weights: EntityWeights | None = None,
) -> dict:
    """weights=None — прежний неоценённый Жаккар (нужен для сравнения в eval/)."""
    signals = {
        "entity_jaccard": (
            weighted_entity_jaccard(entities_a, entities_b, weights)
            if weights is not None
            else entity_jaccard(entities_a, entities_b)
        ),
        "tokens_jaccard": tokens_jaccard(extract_number_date_tokens(_full_text(article_a)), extract_number_date_tokens(_full_text(article_b))),
        "cosine_sim": cosine_sim,
        "time_decay": time_decay(article_a.get("published_at"), article_b.get("published_at")),
    }
    total = (
        WEIGHT_ENTITY_JACCARD * signals["entity_jaccard"]
        + WEIGHT_TOKENS_JACCARD * signals["tokens_jaccard"]
        + WEIGHT_COSINE * signals["cosine_sim"]
        + WEIGHT_TIME_DECAY * signals["time_decay"]
    )
    return {"score": total, "signals": signals}


def score_candidates(
    conn,
    article_id: str,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[dict]:
    target = db.get_article(conn, article_id)
    if target is None or target["embedding"] is None:
        return []

    target_entities = db.entity_ids_by_article(conn, [article_id])[article_id]
    if not target_entities:
        return []

    candidates = db.find_candidate_articles(
        conn,
        article_id,
        target["embedding"],
        list(target_entities),
        window_days,
        target["published_at"],
        limit,
    )
    if not candidates:
        return []

    candidate_entities = db.entity_ids_by_article(conn, [c["id"] for c in candidates])
    weights = get_entity_weights(conn)

    scored = []
    for candidate in candidates:
        result = score_pair(
            target,
            target_entities,
            candidate,
            candidate_entities[candidate["id"]],
            candidate["cosine_sim"],
            weights,
        )
        scored.append({"article": candidate, **result})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored
