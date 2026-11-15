"""Фаза 4 плана P0: IDF-Жаккар сущностей, лимит события, запись сигналов серой зоны."""

from datetime import datetime, timedelta, timezone

import pytest

from hotaru_worker import scoring
from hotaru_worker.scoring import EntityWeights

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def weights(total=1000, **freq):
    return EntityWeights(total, freq)


def test_rare_entity_weighs_more_than_frequent_one():
    w = weights(russia=300, rare=3)
    assert w.weight("rare") > w.weight("russia") > 1.0 - 1e-9


def test_most_frequent_entity_is_not_zeroed():
    assert weights(total=1000, everywhere=1000).weight("everywhere") == pytest.approx(1.0, abs=0.01)


def test_unknown_entity_gets_the_highest_weight():
    w = weights(russia=300)
    assert w.weight("never-seen") > w.weight("russia")


def test_weighted_jaccard_prefers_a_shared_rare_entity():
    w = weights(russia=300, usa=250, rare=2, other1=40, other2=40)
    # Пара делит редкую сущность, но не частые…
    rare_pair = scoring.weighted_entity_jaccard({"rare", "russia"}, {"rare", "usa"}, w)
    # …против пары, делящей только частую.
    frequent_pair = scoring.weighted_entity_jaccard({"russia", "other1"}, {"russia", "other2"}, w)
    assert rare_pair > frequent_pair
    # Обычный Жаккар их не различает: в обоих случаях 1/3.
    assert scoring.entity_jaccard({"rare", "russia"}, {"rare", "usa"}) == pytest.approx(1 / 3)
    assert scoring.entity_jaccard({"russia", "other1"}, {"russia", "other2"}) == pytest.approx(1 / 3)


def test_weighted_jaccard_bounds_and_empty():
    w = weights(a=10, b=10)
    assert scoring.weighted_entity_jaccard({"a"}, {"a"}, w) == pytest.approx(1.0)
    assert scoring.weighted_entity_jaccard({"a"}, {"b"}, w) == 0.0
    assert scoring.weighted_entity_jaccard(set(), set(), w) == 0.0


def test_score_pair_without_weights_keeps_plain_jaccard():
    a = {"title": "x", "lead": None, "body": None, "published_at": T0}
    result = scoring.score_pair(a, {"e1", "e2"}, a, {"e1"}, 0.5)
    assert result["signals"]["entity_jaccard"] == pytest.approx(0.5)


def test_score_pair_with_weights_uses_weighted_jaccard():
    a = {"title": "x", "lead": None, "body": None, "published_at": T0}
    w = weights(e1=2, e2=500)
    plain = scoring.score_pair(a, {"e1", "e2"}, a, {"e1"}, 0.5)["signals"]["entity_jaccard"]
    weighted = scoring.score_pair(a, {"e1", "e2"}, a, {"e1"}, 0.5, w)["signals"]["entity_jaccard"]
    assert weighted > plain  # общая e1 редкая, лишняя e2 частая


# --- жёсткий лимит события -----------------------------------------------------


def test_article_joins_a_short_small_event():
    assert scoring.within_event_limits(5, T0, T0 + timedelta(hours=10), 1, T0 + timedelta(hours=12), T0 + timedelta(hours=12))


def test_article_over_72_hours_from_event_start_is_rejected():
    assert not scoring.within_event_limits(5, T0, T0 + timedelta(hours=10), 1, T0 + timedelta(hours=73), T0 + timedelta(hours=73))


def test_exactly_72_hours_is_allowed():
    assert scoring.within_event_limits(5, T0, T0, 1, T0 + timedelta(hours=72), T0 + timedelta(hours=72))


def test_event_with_40_articles_is_full():
    assert not scoring.within_event_limits(40, T0, T0, 1, T0, T0)
    assert scoring.within_event_limits(39, T0, T0, 1, T0, T0)


def test_article_published_before_event_start_counts_toward_span():
    assert not scoring.within_event_limits(3, T0, T0 + timedelta(hours=70), 1, T0 - timedelta(hours=5), T0 - timedelta(hours=5))


def test_merge_of_two_events_checks_combined_size_and_span():
    assert not scoring.within_event_limits(25, T0, T0 + timedelta(hours=5), 20, T0, T0 + timedelta(hours=5))
    assert not scoring.within_event_limits(
        3, T0, T0 + timedelta(hours=40), 3, T0 + timedelta(hours=50), T0 + timedelta(hours=80)
    )


def test_unknown_publication_time_does_not_block():
    assert scoring.within_event_limits(5, None, None, 1, None, None)
