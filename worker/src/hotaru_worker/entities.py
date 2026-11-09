import logging

import pymorphy3
import spacy
from rapidfuzz import fuzz, process

from . import db

logger = logging.getLogger(__name__)

# kk-модели у spaCy нет, и kk-источников в sources.yaml тоже пока нет (см. CLAUDE.md §8) —
# ru/en покрывают весь текущий поток.
_SPACY_MODELS = {"ru": "ru_core_news_sm", "en": "en_core_web_sm"}

_LABEL_MAP = {
    "ru": {"PER": "person", "ORG": "org", "LOC": "place"},
    "en": {"PERSON": "person", "ORG": "org", "GPE": "place", "LOC": "place"},
}

# OpenCorpora-граммемы, которыми pymorphy3 помечает разборы-кандидаты — используются,
# чтобы среди нескольких равновероятных разборов слова выбрать тот, что соответствует
# типу сущности от spaCy (иначе, например, "Астану" лемматизируется в мужское имя
# "Астан" вместо города "Астана" — оба разбора у pymorphy имеют одинаковый score).
_GRAMMEME_HINT = {"place": "Geox", "org": "Orgn"}

FUZZY_MATCH_THRESHOLD = 92

_morph = pymorphy3.MorphAnalyzer()


def load_models() -> dict[str, spacy.language.Language]:
    return {lang: spacy.load(model_name, exclude=["lemmatizer"]) for lang, model_name in _SPACY_MODELS.items()}


def _best_parse(word: str, entity_type: str):
    parses = _morph.parse(word)
    hint = _GRAMMEME_HINT.get(entity_type)
    if hint:
        for parse in parses:
            if hint in parse.tag:
                return parse
    return parses[0]


def _titlecase(word: str) -> str:
    return word[:1].upper() + word[1:] if word else word


def _normalize_ru_phrase(span, entity_type: str) -> str:
    root = span.root
    root_parse = _best_parse(root.text, entity_type)
    root_inflected = root_parse.inflect({"nomn"})
    root_word = root_inflected.word if root_inflected else root.text.lower()
    root_gender = root_inflected.tag.gender if root_inflected and root_inflected.tag.gender else None

    words = []
    for token in span:
        if token.i == root.i:
            words.append(_titlecase(root_word))
            continue
        # amod/det-модификаторы согласуются в роде с головным словом (root) — их
        # тоже приводим к именительному. Всё остальное (genitive-дополнения вроде
        # "банка КазахстанА", предлоги) — самостоятельные члены фразы, не часть
        # цепочки согласования по падежу, трогать их нельзя.
        if token.head.i == root.i and token.dep_ in ("amod", "det") and token.pos_ in ("ADJ", "DET"):
            parse = _morph.parse(token.text)[0]
            grammemes = {"nomn"}
            if root_gender:
                grammemes.add(root_gender)
            inflected = parse.inflect(grammemes) or parse.inflect({"nomn"})
            word = inflected.word if inflected else token.text.lower()
            words.append(_titlecase(word))
        else:
            words.append(token.text)
    return " ".join(words)


def normalize_entity_name(span, lang: str, entity_type: str) -> str:
    """Раздел 3.3 CLAUDE.md: "Нацбанк"/"НБ РК"/"Национальный банк Казахстана" —
    одна запись, нужно приведение к именительному падежу.

    Для person лемматизация не делается: на реальных данных pymorphy3 уверенно
    (score=1.0) лемматизировал фамилию "Токаев" в "токай" — случайное совпадение
    с несвязанным словарным словом, альтернативы "фамилия" в его словаре просто
    нет. Падежные варианты фамилий по-прежнему схлопываются в resolve_entity()
    нечётким сравнением алиасов (rapidfuzz).
    """
    text = " ".join(span.text.split())
    if not text or lang != "ru" or entity_type == "person":
        return text
    return _normalize_ru_phrase(span, entity_type)


def extract_mentions(nlp, text: str, lang: str) -> list[tuple[str, str]]:
    label_map = _LABEL_MAP[lang]
    mentions = []
    for ent in nlp(text).ents:
        entity_type = label_map.get(ent.label_)
        if not entity_type:
            continue
        canonical_name = normalize_entity_name(ent, lang, entity_type)
        if canonical_name:
            mentions.append((canonical_name, entity_type))
    return mentions


class _AliasCache:
    """Кэш алиасов на время процесса. Раньше каждое упоминание шло в БД
    (find_entity_by_alias ≈0.11 с), а при промахе list_aliases скачивал весь
    список заново (≈0.7 с на ~6000 строк) — ~6–12 с на статью при ~0.35 с
    самого spaCy (замер 2026-11-09). Теперь индекс грузится один раз и
    пополняется при add_alias/create_entity; результат сопоставления тот же
    (точное совпадение без учёта регистра по языку, затем WRatio >= порога по
    алиасам того же языка и типа)."""

    def __init__(self, rows: list[dict]):
        self.exact: dict[tuple[str, str], str] = {}
        self.fuzzy: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
        for row in rows:
            self.add(row["entity_id"], row["alias"], row["lang"], row["type"])

    def add(self, entity_id: str, alias: str, lang: str, entity_type: str) -> None:
        self.exact.setdefault((lang, alias.lower()), entity_id)
        aliases, ids = self.fuzzy.setdefault((lang, entity_type), ([], []))
        aliases.append(alias)
        ids.append(entity_id)


_cache: _AliasCache | None = None
# Самая короткая форма имени person, уже записанная в этом процессе: повторный
# UPDATE с не более короткой формой в БД ничего бы не изменил.
_shortest_person_name: dict[str, int] = {}


def _get_cache(conn) -> _AliasCache:
    global _cache
    if _cache is None:
        _cache = _AliasCache(db.load_alias_index(conn))
    return _cache


def reset_cache() -> None:
    global _cache
    _cache = None
    _shortest_person_name.clear()


def resolve_entity(conn, canonical_name: str, lang: str, entity_type: str) -> str:
    cache = _get_cache(conn)
    existing = cache.exact.get((lang, canonical_name.lower()))
    if existing:
        _maybe_fix_person_canonical_name(conn, existing, canonical_name, entity_type)
        return existing

    aliases, ids = cache.fuzzy.get((lang, entity_type), ([], []))
    if aliases:
        match = process.extractOne(
            canonical_name,
            aliases,
            scorer=fuzz.WRatio,
            score_cutoff=FUZZY_MATCH_THRESHOLD,
        )
        if match:
            _, _, idx = match
            entity_id = ids[idx]
            db.add_alias(conn, entity_id, canonical_name, lang)
            cache.add(entity_id, canonical_name, lang, entity_type)
            _maybe_fix_person_canonical_name(conn, entity_id, canonical_name, entity_type)
            return entity_id

    entity_id = db.create_entity(conn, canonical_name, entity_type)
    db.add_alias(conn, entity_id, canonical_name, lang)
    cache.add(entity_id, canonical_name, lang, entity_type)
    return entity_id


def _maybe_fix_person_canonical_name(conn, entity_id: str, candidate_name: str, entity_type: str) -> None:
    """person не проходит pymorphy-лемматизацию (см. normalize_entity_name), поэтому
    canonical_name фиксируется по первому увиденному падежу — не обязательно
    именительному ("Владимира Путина" вместо "Владимир Путин"). Падежные окончания
    в русском почти всегда ДОБАВЛЯЮТ буквы к именительной форме, поэтому более
    короткий вариант того же алиаса — надёжный сигнал, что он ближе к именительному."""
    if entity_type == "person":
        seen = _shortest_person_name.get(entity_id)
        if seen is not None and len(candidate_name) >= seen:
            return
        db.shorten_canonical_name_if_needed(conn, entity_id, candidate_name)
        _shortest_person_name[entity_id] = len(candidate_name)


def process_article(conn, models: dict, article: dict) -> int:
    lang = article["lang"]
    nlp = models.get(lang)
    if nlp is None:
        db.mark_entities_extracted(conn, article["id"])
        return 0

    text = "\n".join(part for part in (article["title"], article["lead"], article["body"]) if part)
    mentions = extract_mentions(nlp, text, lang)

    # Одно и то же (имя, тип) в статье встречается много раз — разбираем один раз.
    mention_counts: dict[tuple[str, str], int] = {}
    for mention in mentions:
        mention_counts[mention] = mention_counts.get(mention, 0) + 1

    counts: dict[str, int] = {}
    for (canonical_name, entity_type), n in mention_counts.items():
        entity_id = resolve_entity(conn, canonical_name, lang, entity_type)
        counts[entity_id] = counts.get(entity_id, 0) + n

    db.upsert_article_entities(conn, article["id"], counts)

    db.mark_entities_extracted(conn, article["id"])
    return len(counts)
