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


def resolve_entity(conn, canonical_name: str, lang: str, entity_type: str) -> str:
    existing = db.find_entity_by_alias(conn, canonical_name, lang)
    if existing:
        _maybe_fix_person_canonical_name(conn, existing, canonical_name, entity_type)
        return existing

    candidates = db.list_aliases(conn, lang, entity_type)
    if candidates:
        match = process.extractOne(
            canonical_name,
            [c["alias"] for c in candidates],
            scorer=fuzz.WRatio,
            score_cutoff=FUZZY_MATCH_THRESHOLD,
        )
        if match:
            _, _, idx = match
            entity_id = candidates[idx]["entity_id"]
            db.add_alias(conn, entity_id, canonical_name, lang)
            _maybe_fix_person_canonical_name(conn, entity_id, canonical_name, entity_type)
            return entity_id

    entity_id = db.create_entity(conn, canonical_name, entity_type)
    db.add_alias(conn, entity_id, canonical_name, lang)
    return entity_id


def _maybe_fix_person_canonical_name(conn, entity_id: str, candidate_name: str, entity_type: str) -> None:
    """person не проходит pymorphy-лемматизацию (см. normalize_entity_name), поэтому
    canonical_name фиксируется по первому увиденному падежу — не обязательно
    именительному ("Владимира Путина" вместо "Владимир Путин"). Падежные окончания
    в русском почти всегда ДОБАВЛЯЮТ буквы к именительной форме, поэтому более
    короткий вариант того же алиаса — надёжный сигнал, что он ближе к именительному."""
    if entity_type == "person":
        db.shorten_canonical_name_if_needed(conn, entity_id, candidate_name)


def process_article(conn, models: dict, article: dict) -> int:
    lang = article["lang"]
    nlp = models.get(lang)
    if nlp is None:
        db.mark_entities_extracted(conn, article["id"])
        return 0

    text = "\n".join(part for part in (article["title"], article["lead"], article["body"]) if part)
    mentions = extract_mentions(nlp, text, lang)

    counts: dict[str, int] = {}
    for canonical_name, entity_type in mentions:
        entity_id = resolve_entity(conn, canonical_name, lang, entity_type)
        counts[entity_id] = counts.get(entity_id, 0) + 1

    for entity_id, count in counts.items():
        db.upsert_article_entity(conn, article["id"], entity_id, count)

    db.mark_entities_extracted(conn, article["id"])
    return len(counts)
