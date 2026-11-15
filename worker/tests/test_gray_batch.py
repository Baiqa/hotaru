"""Фаза 5.4 плана P0: пакетная классификация серой зоны (без обращений к LLM и БД)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from hotaru_worker import clustering, db, gray_zone, scoring
from hotaru_worker.gray_zone import Verdict, parse_batch_response

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


# --- разбор ответа ----------------------------------------------------------------


def test_parse_object_with_verdicts():
    raw = json.dumps({"verdicts": [{"id": 1, "verdict": "same"}, {"id": 2, "verdict": "different"}]})
    assert parse_batch_response(raw, 2) == [Verdict.SAME, Verdict.DIFFERENT]


def test_parse_bare_list():
    assert parse_batch_response('[{"id": 1, "verdict": "related"}]', 1) == [Verdict.RELATED]


def test_missing_pair_stays_unanswered():
    raw = json.dumps({"verdicts": [{"id": 2, "verdict": "same"}]})
    assert parse_batch_response(raw, 3) == [None, Verdict.SAME, None]


def test_garbage_items_are_ignored_not_guessed():
    raw = json.dumps(
        {"verdicts": [{"id": 1, "verdict": "maybe"}, {"id": 9, "verdict": "same"}, {"verdict": "same"}, "x", {"id": "2", "verdict": "same"}]}
    )
    # непонятный вердикт, номер вне диапазона, нет id, не словарь — всё пропускается;
    # id строкой «2» — допустимая запись пары 2.
    assert parse_batch_response(raw, 2) == [None, Verdict.SAME]


def test_first_answer_for_a_pair_wins():
    raw = json.dumps({"verdicts": [{"id": 1, "verdict": "different"}, {"id": 1, "verdict": "same"}]})
    assert parse_batch_response(raw, 1) == [Verdict.DIFFERENT]


@pytest.mark.parametrize("raw", ["", "не json", "null", '{"verdicts": "x"}', "42"])
def test_unparseable_answer_gives_no_verdicts(raw):
    assert parse_batch_response(raw, 2) == [None, None]


def test_batch_prompt_numbers_every_pair():
    text = gray_zone._format_batch_pairs(
        [({"title": "A1", "lead": "a1"}, {"title": "B1", "lead": "b1"}), ({"title": "A2", "lead": None}, {"title": "B2", "lead": ""})]
    )
    assert "Пара 1" in text and "Пара 2" in text and "A2" in text and "B2" in text


# --- предварительный проход -----------------------------------------------------------


def candidate(n, score, published=T0):
    return {
        "article": {"id": f"cand{n}", "title": f"cand {n}", "lead": "", "published_at": published},
        "score": score,
        "signals": {"entity_jaccard": 0.5, "tokens_jaccard": 0.1, "cosine_sim": 0.7, "time_decay": 0.9},
    }


class Env:
    """Подмена БД и LLM: что считается серой зоной, что уже в кеше, что сохранено."""

    def __init__(self, monkeypatch, scored, cached=(), fits=True):
        self.saved = []
        self.batches = []
        self.cached = set(cached)
        monkeypatch.setattr(db, "event_id_for_article", lambda conn, aid: None)
        monkeypatch.setattr(scoring, "score_candidates", lambda conn, aid: scored[aid])
        monkeypatch.setattr(db, "get_gray_zone_verdict", lambda conn, a, b: "same" if tuple(sorted((str(a), str(b)))) in self.cached else None)
        monkeypatch.setattr(clustering, "_fits_event_limits", lambda conn, article, cand: fits)
        monkeypatch.setattr(db, "save_gray_zone_verdict", lambda conn, a, b, v, score=None, signals=None: self.saved.append((a, b, v, score)))
        monkeypatch.setattr(gray_zone, "classify_batch", self.classify)
        monkeypatch.setattr(clustering.runlog, "out_of_time", lambda: False)

    def classify(self, client, pairs):
        self.batches.append(len(pairs))
        return [Verdict.SAME] * len(pairs)


def article(n):
    return {"id": f"art{n}", "title": f"art {n}", "lead": "", "published_at": T0}


def test_only_gray_zone_pairs_go_to_the_llm(monkeypatch):
    env = Env(monkeypatch, {"art1": [candidate(1, 0.9), candidate(2, 0.5)], "art2": [candidate(3, 0.2)]})
    clustering.prefetch_gray_zone(None, None, [article(1), article(2)], 12)
    # 0.9 — присоединение без LLM, 0.2 — новое событие без LLM, 0.5 — серая зона
    assert env.batches == [1]
    assert [s[1] for s in env.saved] == ["cand2"]


def test_pairs_are_packed_by_batch_size(monkeypatch):
    scored = {f"art{n}": [candidate(n, 0.5)] for n in range(1, 8)}
    env = Env(monkeypatch, scored)
    clustering.prefetch_gray_zone(None, None, [article(n) for n in range(1, 8)], 3)
    assert env.batches == [3, 3, 1]


def test_only_top_candidate_tries_are_considered(monkeypatch):
    scored = {"art1": [candidate(n, 0.5) for n in range(1, 6)]}
    env = Env(monkeypatch, scored)
    clustering.prefetch_gray_zone(None, None, [article(1)], 12)
    assert env.batches == [clustering.CANDIDATE_TRIES]


def test_cached_pair_is_not_asked_again(monkeypatch):
    env = Env(monkeypatch, {"art1": [candidate(1, 0.5)]}, cached=[("art1", "cand1")])
    clustering.prefetch_gray_zone(None, None, [article(1)], 12)
    assert env.batches == []


def test_same_pair_from_both_sides_is_asked_once(monkeypatch):
    a, b = article(1), article(2)
    scored = {
        "art1": [{**candidate(0, 0.5), "article": {**b}}],
        "art2": [{**candidate(0, 0.5), "article": {**a}}],
    }
    env = Env(monkeypatch, scored)
    clustering.prefetch_gray_zone(None, None, [a, b], 12)
    assert env.batches == [1]


def test_pair_that_cannot_join_because_of_event_limits_is_not_asked(monkeypatch):
    env = Env(monkeypatch, {"art1": [candidate(1, 0.5)]}, fits=False)
    clustering.prefetch_gray_zone(None, None, [article(1)], 12)
    assert env.batches == []


def test_saved_verdict_carries_score_and_prefetched_scores_are_returned(monkeypatch):
    scored = {"art1": [candidate(1, 0.5)]}
    env = Env(monkeypatch, scored)
    result = clustering.prefetch_gray_zone(None, None, [article(1)], 12)
    assert env.saved == [("art1", "cand1", "same", 0.5)]
    assert result == {"art1": scored["art1"]}


def test_unanswered_pair_is_not_saved(monkeypatch):
    env = Env(monkeypatch, {"art1": [candidate(1, 0.5)]})
    monkeypatch.setattr(gray_zone, "classify_batch", lambda client, pairs: [None] * len(pairs))
    clustering.prefetch_gray_zone(None, None, [article(1)], 12)
    assert env.saved == []


def test_time_budget_stops_the_prefetch(monkeypatch):
    scored = {f"art{n}": [candidate(n, 0.5)] for n in range(1, 5)}
    env = Env(monkeypatch, scored)
    monkeypatch.setattr(clustering.runlog, "out_of_time", lambda: True)
    clustering.prefetch_gray_zone(None, None, [article(n) for n in range(1, 5)], 2)
    assert env.batches == []


def test_threshold_boundaries_are_gray(monkeypatch):
    lo, hi = clustering.LOWER_THRESHOLD, clustering.UPPER_THRESHOLD
    assert clustering._is_gray(lo) and clustering._is_gray(hi)
    assert not clustering._is_gray(lo - 1e-9) and not clustering._is_gray(hi + 1e-9)
