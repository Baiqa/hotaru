"""Фаза 4 плана P0: нормализация сущностей и вырезание служебных строк.

Тесты с `nlp` грузят русскую модель spaCy (~несколько секунд).
"""

import pytest
import spacy

from hotaru_worker import entities


@pytest.fixture(scope="module")
def nlp():
    return spacy.load("ru_core_news_sm", exclude=["lemmatizer"])


@pytest.fixture(autouse=True)
def known_tokens():
    entities._known_person_tokens.clear()
    entities.seed_known_person_tokens(["Фридрих Мерц", "Эммануэль Макрон", "Стив Уиткофф"])
    yield
    entities._known_person_tokens.clear()


def names(nlp, text, entity_type=None):
    return [n for n, t in entities.extract_mentions(nlp, text, "ru") if entity_type in (None, t)]


# --- служебные строки ---------------------------------------------------------


def test_ria_dateline_is_removed():
    text = "- Тезис.\nМОСКВА, 13 ноя - РИА Новости. Путин встретился."
    assert entities.strip_service_lines(text) == "- Тезис.\nПутин встретился."


def test_ria_dateline_with_em_dash_and_other_city():
    assert entities.strip_service_lines("КИШИНЕВ, 15 ноя — РИА Новости. Текст.") == "Текст."


def test_interfax_dateline_is_removed():
    text = "Заголовок\nМосква. 15 ноября. INTERFAX.RU - Китай и США ведут работу."
    assert entities.strip_service_lines(text) == "Заголовок\nКитай и США ведут работу."


def test_tass_dateline_is_removed():
    assert entities.strip_service_lines("МОСКВА, 15 ноября. /ТАСС/. Текст.") == "Текст."


def test_dateline_in_the_middle_of_a_line_is_not_touched():
    text = "Сообщение прислали из Москвы, 13 ноя - РИА Новости. Дальше."
    assert entities.strip_service_lines(text) == text


def test_plain_text_is_unchanged():
    text = "Президент России Владимир Путин провёл встречу."
    assert entities.strip_service_lines(text) == text


def test_dateline_city_and_agency_do_not_become_entities(nlp):
    found = names(nlp, "МОСКВА, 13 ноя - РИА Новости. Лавров заявил, что переговоры продолжатся.")
    assert "Москва" not in found
    assert not any("Новост" in n for n in found)
    assert "Лавров" in found


def test_publisher_mention_in_text_is_not_an_entity(nlp):
    assert not any("ТАСС" in n.upper() for n in names(nlp, "Об этом сообщило агентство ТАСС со ссылкой на источник."))


def test_publisher_stoplist_ignores_case_and_quotes():
    assert entities.is_publisher_name("РИА Новости")
    assert entities.is_publisher_name("«ТАСС»")
    assert entities.is_publisher_name("риа новости")
    assert not entities.is_publisher_name("Минобороны")


# --- фамилии в косвенных падежах ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Заявление Трампа вызвало реакцию.",
        "Встреча с Трампом состоялась вчера.",
        "Лавров передал письмо Трампу.",
        "Говорил о Трампе.",
        "Трамп заявил, что готов.",
    ],
)
def test_all_forms_of_trump_collapse_to_one_entity(nlp, text):
    assert "Трамп" in names(nlp, text, "person")
    assert not [n for n in names(nlp, text) if n.startswith("Трамп") and n != "Трамп"]


def test_feminine_surname_keeps_gender(nlp):
    assert names(nlp, "Слова Марии Захаровой прозвучали.") == ["Мария Захарова"]
    assert names(nlp, "Представитель МИД Мария Захарова заявила.", "person") == ["Мария Захарова"]
    assert names(nlp, "Победа Андреевой.") == ["Андреева"]


def test_nominative_surname_is_untouched(nlp):
    # pymorphy знает «Токаев» только как слово «токай» — трогать именительный нельзя.
    assert names(nlp, "Токаев провел встречу.") == ["Токаев"]
    assert names(nlp, "Заявление Токаева опубликовано.") == ["Токаев"]
    assert names(nlp, "Встреча с Токаевым.") == ["Токаев"]


def test_full_name_in_genitive(nlp):
    assert names(nlp, "Слова Сергея Лаврова.") == ["Сергей Лавров"]
    assert names(nlp, "Встреча с Зеленским.") == ["Зеленский"]


def test_surname_missing_from_dictionary_is_cut_only_if_stem_is_known(nlp):
    assert names(nlp, "Заявление Мерца.") == ["Мерц"]
    assert names(nlp, "Слова Макрона.") == ["Макрон"]
    entities._known_person_tokens.clear()
    # основа «Мерц» неизвестна — не угадываем, оставляем как есть
    assert names(nlp, "Заявление Мерца.") == ["Мерца"]


def test_leading_noun_captured_by_spacy_is_dropped(nlp):
    assert names(nlp, "Ответил Мишустин.") == ["Мишустин"]


def test_organization_acronyms_keep_uppercase(nlp):
    found = names(nlp, "ЕС и США обсудили сделку, сообщил МИД.")
    assert {"ЕС", "США", "МИД"} <= set(found)


def test_hyphenated_place_keeps_its_hyphen(nlp):
    assert "Абу-Даби" in names(nlp, "Переговоры прошли в Абу-Даби.", "place")


def test_person_span_is_cut_at_a_spaced_dash(nlp):
    doc = nlp("Трамп - республиканцы - заявили об этом.")
    span = doc[0:4]  # «Трамп - республиканцы -» — так spaCy иногда захватывает фразу целиком
    assert entities._normalize_ru_person(span) == "Трамп"


def test_full_name_inflects_together_so_nominative_surname_is_kept(nlp):
    # spaCy-small видит у фамилии Gen/Masc, хотя имя Nom/Fem: женскую фамилию нельзя «срезать».
    text = "Блогер Лерчек (Валерия Чекалина) обратилась к президенту России."
    assert "Валерия Чекалина" in names(nlp, text, "person")
