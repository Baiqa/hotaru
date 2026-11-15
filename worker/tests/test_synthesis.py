"""Фаза 5 плана P0: пост-проверка синтеза (раздел 3.4 CLAUDE.md) и имя модели в результате."""

import pytest

from hotaru_worker import synthesis
from hotaru_worker.synthesis import _Sentence, _SynthesisResponse

FACTS = [
    {
        "claim_id": "c1",
        "text": "Нацбанк сохранил базовую ставку",
        "evidence": "Национальный банк принял решение сохранить базовую ставку на уровне 16,5 процента годовых",
        "type": "fact",
        "source_name": "A",
        "bucket": "confirmed",
        "numbers": [{"value": 16.5, "unit": "%", "label": "ставка", "period": None}],
    },
    {
        "claim_id": "c2",
        "text": "Решение принято в пятницу",
        "evidence": "Решение принято в пятницу",
        "type": "fact",
        "source_name": "B",
        "bucket": "unique",
        "numbers": [],
    },
]


def fake_model(monkeypatch, *responses, model="test-model"):
    calls = []
    queue = list(responses)

    def _call(client, prompt):
        calls.append(prompt)
        return queue.pop(0), model

    monkeypatch.setattr(synthesis, "_call_model", _call)
    return calls


def response(*sentences):
    return _SynthesisResponse(sentences=[_Sentence(text=t, claim_ids=ids) for t, ids in sentences])


def render(monkeypatch, *responses, **kw):
    fake_model(monkeypatch, *responses, **kw)
    return synthesis.generate_render(None, "e1", FACTS, "summary_30s")


def test_valid_text_is_returned_with_model_name(monkeypatch):
    body, claims, model = render(
        monkeypatch,
        response(("Ставка сохранена на уровне 16,5%.", ["c1"]), ("Об этом сообщили в пятницу.", ["c2"])),
        model="gemini-x",
    )
    assert body == "Ставка сохранена на уровне 16,5%.\nОб этом сообщили в пятницу."
    assert claims == [["c1"], ["c2"]]
    assert model == "gemini-x"


def test_sentence_without_claim_id_is_dropped(monkeypatch):
    body, claims, _ = render(monkeypatch, response(("Предложение без опоры.", []), ("Решение в пятницу.", ["c2"])))
    assert body == "Решение в пятницу."
    assert claims == [["c2"]]


def test_unknown_claim_id_does_not_count_as_a_citation(monkeypatch):
    assert render(monkeypatch, response(("Выдуманная ссылка.", ["no-such-claim"]))) is None


def test_only_known_claim_ids_are_kept(monkeypatch):
    _, claims, _ = render(monkeypatch, response(("Решение.", ["c2", "ghost"])))
    assert claims == [["c2"]]


def test_sentence_copying_the_original_is_dropped(monkeypatch):
    copied = "Национальный банк принял решение сохранить базовую ставку на уровне 16,5 процента годовых"
    body, _, _ = render(monkeypatch, response((copied, ["c1"]), ("Решение в пятницу.", ["c2"])))
    assert body == "Решение в пятницу."


def test_copy_of_exactly_eight_words_is_caught_seven_is_not(monkeypatch):
    eight = "Национальный банк принял решение сохранить базовую ставку на"
    seven = "Национальный банк принял решение сохранить базовую ставку"
    assert render(monkeypatch, response((eight, ["c1"]))) is None
    body, _, _ = render(monkeypatch, response((seven, ["c1"])))
    assert body == seven


def test_number_missing_from_facts_rejects_generation_and_retries(monkeypatch):
    calls = fake_model(
        monkeypatch,
        response(("Ставка составила 17%.", ["c1"])),
        response(("Ставка составила 16,5%.", ["c1"])),
    )
    body, _, _ = synthesis.generate_render(None, "e1", FACTS, "summary_30s")
    assert body == "Ставка составила 16,5%."
    assert len(calls) == 2


def test_number_missing_after_all_retries_returns_none(monkeypatch):
    fake_model(monkeypatch, *[response(("Ставка 17%.", ["c1"]))] * synthesis.MAX_GENERATION_RETRIES)
    assert synthesis.generate_render(None, "e1", FACTS, "summary_30s") is None


def test_year_like_numbers_are_allowed(monkeypatch):
    body, _, _ = render(monkeypatch, response(("Решение принято в 2026 году.", ["c2"])))
    assert "2026" in body


def test_no_sentences_left_returns_none(monkeypatch):
    assert render(monkeypatch, response(("Без ссылки.", []))) is None


def test_number_formatting_drops_trailing_zero():
    assert synthesis._format_number(165.0) == "165"
    assert synthesis._format_number(16.5) == "16.5"


@pytest.mark.parametrize("raw", ['{"sentences": [{"text": "a", "claim_ids": ["c1"]}]}', '[{"text": "a", "claim_ids": ["c1"]}]', '{"text": "a", "claim_ids": ["c1"]}'])
def test_parser_accepts_all_shapes_groq_returns(raw):
    assert synthesis._parse_synthesis_json(raw).sentences[0].claim_ids == ["c1"]
