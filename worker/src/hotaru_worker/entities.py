import logging
import re

import pymorphy3
import spacy
import yaml
from rapidfuzz import fuzz, process

from . import config, db

logger = logging.getLogger(__name__)

# Служебные строки в начале текста: пометка агентства с городом и датой. Они попадают в
# NER как сущности («МОСКВА» → org, «Риа Новости» → org) и делают общей сущность у трети
# русскоязычного потока (замер 2026-11-15, окно 7 дней: Москва — 2373 статьи, Риа Новости
# — 2271 из 6321). Это не содержание статьи, поэтому строка вырезается целиком до NER.
# Формы проверены по 4874 русскоязычным текстам за 30 дней.
_SERVICE_LINE_RES = (
    # РИА Новости: «МОСКВА, 9 ноя - РИА Новости.», также «—», «Sputnik», «, Имя Фамилия.»
    re.compile(
        r"^[А-ЯЁ][А-ЯЁ0-9 .,()\-–—/«»]{1,60}?, \d{1,2} [а-яё]{3,8}\.? ?[-–—] "
        r"(?:РИА Новости|ТАСС|Sputnik)[^.\n]{0,60}\.[ \t]*",
        re.MULTILINE,
    ),
    # Интерфакс: «Москва. 15 ноября. INTERFAX.RU - »
    re.compile(
        r"^[А-ЯЁ][А-Яа-яё .\-]{1,40}\. \d{1,2} [а-яё]{3,8}\. INTERFAX\.RU[ \t]*[-–—][ \t]*",
        re.MULTILINE,
    ),
    # ТАСС: «МОСКВА, 15 ноября. /ТАСС/.»
    re.compile(r"^[А-ЯЁ][А-ЯЁ .\-]{1,40}, \d{1,2} [а-яё]{3,8}\. /ТАСС/\.[ \t]*", re.MULTILINE),
)


def strip_service_lines(text: str) -> str:
    for pattern in _SERVICE_LINE_RES:
        text = pattern.sub("", text)
    return text


def _load_publisher_stoplist() -> frozenset[str]:
    with open(config.PACKAGE_DIR / "entity_stoplist.yaml", "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return frozenset(_stop_key(name) for name in data["publishers"])


def _stop_key(name: str) -> str:
    return " ".join(name.strip(" \"'«»„“”").lower().split())

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


_PUBLISHERS = _load_publisher_stoplist()

_NAME_GRAMMEMES = {"Surn", "Name", "Patr"}
_SPACY_TO_PYMORPHY_CASE = {"Nom": "nomn", "Gen": "gent", "Dat": "datv", "Acc": "accs", "Ins": "ablt", "Loc": "loct"}
_SPACY_TO_PYMORPHY_GENDER = {"Masc": "masc", "Fem": "femn"}
# Падежные окончания мужских фамилий, которых нет в словаре pymorphy («Мерца», «Макрона»,
# «Уиткоффом»). Отрезаются, только если основа уже известна как имя человека.
_MASC_CASE_SUFFIXES = {
    "Gen": ("а", "я"),
    "Acc": ("а", "я"),
    "Dat": ("у", "ю"),
    "Ins": ("ом", "ем", "ым", "им"),
    "Loc": ("е",),
}

# Слова, которые уже видели в именительном падеже в составе имени человека. Наполняется по
# ходу работы (_remember_person_token) и при старте из БД (seed_known_person_tokens).
_known_person_tokens: set[str] = set()


def seed_known_person_tokens(names: list[str]) -> None:
    for name in names:
        for part in name.split():
            if len(part) >= 3 and part[:1].isupper():
                _known_person_tokens.add(part.lower())


def _remember_person_token(text: str) -> None:
    if len(text) >= 3 and text[:1].isupper() and not text.isupper():
        _known_person_tokens.add(text.lower())


def _has_name_parse(word: str) -> bool:
    return any(_NAME_GRAMMEMES & set(parse.tag.grammemes) for parse in _morph.parse(word))


def _person_tokens(span) -> list:
    """Токены имени без случайно захваченных слов. spaCy-small иногда включает в PER
    соседнее слово: «Ответил Мишустин», «Победа Андреевой». Отбрасываем с краёв только
    глаголы/наречия/предлоги и — с левого края — неодушевлённое существительное, у
    которого в словаре нет разбора как имя («Победа»)."""
    tokens = list(span)
    # Тире, отбитое пробелами («Трамп - республиканцы -»), — конец имени: дальше уже другая
    # фраза. Дефис внутри имени («Жан-Клод») пробелами не отбит и границей не считается.
    for i in range(1, len(tokens)):
        if tokens[i].text in ("-", "–", "—") and tokens[i - 1].whitespace_:
            tokens = tokens[:i]
            break

    def is_junk(token, leading: bool) -> bool:
        if token.pos_ in ("VERB", "AUX", "ADV", "ADP", "PART", "CCONJ", "SCONJ"):
            return True
        return (
            leading
            and token.pos_ == "NOUN"
            and "Inan" in token.morph.get("Animacy")
            and not _has_name_parse(token.text)
        )

    while len(tokens) > 1 and is_junk(tokens[0], leading=True):
        tokens.pop(0)
    while len(tokens) > 1 and is_junk(tokens[-1], leading=False):
        tokens.pop()
    return tokens


def _nominative_person_token(token, case: str | None, gender: str | None) -> str:
    """Имя/фамилия/отчество в именительный падеж. Падеж и род берём у spaCy (на «Трампа»
    он верно даёт Gen/Masc), форму — у pymorphy3 из разбора с граммемой Surn/Name/Patr:
    без этого «Токаева» лемматизировалось бы в женское имя «Токаева». Именительный падеж
    не трогаем («Токаев» pymorphy знает как слово «токай»).

    case/gender — не самого токена, а опорного слова всего имени (_normalize_ru_person):
    полное имя склоняется целиком, а spaCy-small для «flat:name»-токенов даёт собственные,
    нередко ошибочные значения («Валерия Чекалина»: имя Nom/Fem, фамилия Gen/Masc — и
    фамилию превращало в «Чекалин»)."""
    text = token.text
    if case in (None, "Nom") or case not in _SPACY_TO_PYMORPHY_CASE:
        _remember_person_token(text)
        return text
    if text.isupper() or "-" in text or len(text) < 3 or not text[:1].isupper():
        return text

    gender = _SPACY_TO_PYMORPHY_GENDER.get(gender)
    want_case = _SPACY_TO_PYMORPHY_CASE[case]
    for parse in _morph.parse(text):
        tag = parse.tag
        if not (_NAME_GRAMMEMES & set(tag.grammemes)) or tag.number != "sing" or tag.case != want_case:
            continue
        # Общий род (ms-f, «Соболенко») pymorphy отдаёт как gender=None.
        if gender and tag.gender is not None and tag.gender != gender:
            continue
        inflected = parse.inflect({"nomn"})
        if inflected:
            return _titlecase(inflected.word)

    if gender == "masc":
        for suffix in _MASC_CASE_SUFFIXES.get(case, ()):
            stem = text[: -len(suffix)] if text.lower().endswith(suffix) else ""
            if len(stem) >= 3 and stem.lower() in _known_person_tokens:
                return stem
    return text


def _join_tokens(tokens: list, words: list[str]) -> str:
    out = []
    for i, (token, word) in enumerate(zip(tokens, words)):
        out.append(word)
        if i < len(tokens) - 1:
            out.append(token.whitespace_)
    return " ".join("".join(out).split())


def _normalize_ru_person(span) -> str:
    tokens = _person_tokens(span)
    anchor = span.root if any(t.i == span.root.i for t in tokens) else tokens[0]
    case = (anchor.morph.get("Case") or [None])[0]
    gender = (anchor.morph.get("Gender") or [None])[0]
    return _join_tokens(tokens, [_nominative_person_token(token, case, gender) for token in tokens])


def _normalize_ru_phrase(span, entity_type: str) -> str:
    root = span.root
    root_parse = _best_parse(root.text, entity_type)
    root_inflected = root_parse.inflect({"nomn"})
    root_word = root_inflected.word if root_inflected else root.text.lower()
    # Аббревиатура: «США», «МИД», «ЕС» — pymorphy выдаёт лемму строчными, и она
    # превращалась в «Сша», «Мид», «Ес». Слово целиком прописное — оставляем как есть.
    if root.text.isupper() and len(root.text) > 1:
        root_word = root.text
    root_gender = root_inflected.tag.gender if root_inflected and root_inflected.tag.gender else None

    tokens = list(span)
    words = []
    for token in tokens:
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
    return _join_tokens(tokens, words)


def normalize_entity_name(span, lang: str, entity_type: str) -> str:
    """Раздел 3.3 CLAUDE.md: "Нацбанк"/"НБ РК"/"Национальный банк Казахстана" —
    одна запись, нужно приведение к именительному падежу.

    Для person лемматизация словарём не годится: pymorphy3 уверенно (score=1.0)
    лемматизировал фамилию "Токаев" в "токай" — случайное совпадение с несвязанным
    словарным словом. Поэтому person приводится по-другому (_normalize_ru_person):
    падеж и род берём у spaCy, форму — только из разборов pymorphy с граммемой
    Surn/Name/Patr, а именительный падеж не трогаем. Раньше падежные формы
    ("Трамп", "Трампа", "Трампом", "Трампу") оставались четырьмя разными сущностями.
    """
    text = " ".join(span.text.split())
    if not text or lang != "ru":
        return text
    if entity_type == "person":
        return _normalize_ru_person(span) or text
    return _normalize_ru_phrase(span, entity_type)


def is_publisher_name(name: str) -> bool:
    return _stop_key(name) in _PUBLISHERS


def extract_mentions(nlp, text: str, lang: str) -> list[tuple[str, str]]:
    label_map = _LABEL_MAP[lang]
    mentions = []
    for ent in nlp(strip_service_lines(text)).ents:
        entity_type = label_map.get(ent.label_)
        if not entity_type:
            continue
        canonical_name = normalize_entity_name(ent, lang, entity_type)
        if canonical_name and not is_publisher_name(canonical_name):
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


_known_tokens_seeded = False


def _ensure_known_person_tokens(conn) -> None:
    global _known_tokens_seeded
    if not _known_tokens_seeded:
        seed_known_person_tokens(db.person_canonical_names(conn))
        _known_tokens_seeded = True


def process_article(conn, models: dict, article: dict) -> int:
    lang = article["lang"]
    nlp = models.get(lang)
    if nlp is None:
        db.mark_entities_extracted(conn, article["id"])
        return 0

    _ensure_known_person_tokens(conn)

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
