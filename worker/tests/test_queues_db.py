"""Фаза 5 плана P0: очереди синтеза и фактов, триггер пересчёта события — на настоящей БД.

Каждый тест работает внутри транзакции, которая откатывается, поэтому в БД ничего не
остаётся. Тесты пропускаются, если DATABASE_URL недоступен (CI их не запускает).
Очередь возвращает и настоящие события, поэтому проверяется только положение
созданных тестом событий.
"""

import os
import uuid

import pytest
from dotenv import load_dotenv

from hotaru_worker import db

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="нужен DATABASE_URL")


@pytest.fixture
def conn():
    with db.connect(DATABASE_URL) as connection:
        connection.autocommit = False
        try:
            yield connection
        finally:
            connection.rollback()


def make_source(conn) -> str:
    key = uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(
            "insert into sources (name, base_url, fetch_method, lang) values (%s, %s, 'rss', 'ru') returning id",
            (f"t-{key}", f"https://{key}.example"),
        )
        return cur.fetchone()["id"]


def make_article(conn, source_id, age_days: float, claim: bool = True, attempts: int = 0) -> str:
    key = uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into articles (source_id, url, canonical_url, title, body, content_hash, fetch_status,
                                  published_at, claims_attempts)
            values (%s, %s, %s, 'тест', 'текст', %s, 'ok', now() - make_interval(secs => %s), %s)
            returning id
            """,
            (source_id, f"https://t.example/{key}", f"https://t.example/{key}", key, age_days * 86400, attempts),
        )
        article_id = cur.fetchone()["id"]
        if claim:
            cur.execute(
                "insert into claims (article_id, text, type, evidence) values (%s, 'факт', 'fact', 'цитата')",
                (article_id,),
            )
        return article_id


def make_event(conn, sources: int = 1, age_days: float = 0.1, claim: bool = True, article_attempts: int = 0) -> str:
    """Событие с `sources` статьями от разных изданий; последняя статья age_days назад."""
    with conn.cursor() as cur:
        cur.execute("insert into events (title, started_at) values ('тест', now()) returning id")
        event_id = cur.fetchone()["id"]
    for i in range(sources):
        article_id = make_article(conn, make_source(conn), age_days + i * 0.001, claim, article_attempts)
        with conn.cursor() as cur:
            cur.execute("insert into event_articles (article_id, event_id) values (%s, %s)", (article_id, event_id))
    return event_id


def queue_ids(conn, **kwargs) -> list:
    return [e["id"] for e in db.events_missing_render(conn, 10_000, **kwargs)]


def set_event(conn, event_id, **fields):
    assignments = ", ".join(f"{k} = %s" for k in fields)
    with conn.cursor() as cur:
        cur.execute(f"update events set {assignments} where id = %s", (*fields.values(), event_id))


# --- триггер пересчёта ---------------------------------------------------------


def test_trigger_counts_sources_and_last_article(conn):
    event_id = make_event(conn, sources=3, age_days=1)
    with conn.cursor() as cur:
        cur.execute(
            "select source_count, now() - last_article_at as age from events where id = %s", (event_id,)
        )
        row = cur.fetchone()
    assert row["source_count"] == 3
    assert 0.9 * 86400 < row["age"].total_seconds() < 1.1 * 86400


def test_trigger_follows_articles_moved_between_events(conn):
    first, second = make_event(conn, sources=2), make_event(conn, sources=1)
    with conn.cursor() as cur:
        cur.execute("select article_id from event_articles where event_id = %s limit 1", (first,))
        moved = cur.fetchone()["article_id"]
        cur.execute("update event_articles set event_id = %s where article_id = %s", (second, moved))
        cur.execute("select id, source_count from events where id = any(%s)", ([first, second],))
        counts = {r["id"]: r["source_count"] for r in cur.fetchall()}
    assert counts == {first: 1, second: 2}


# --- очередь синтеза -----------------------------------------------------------


def test_multi_source_event_goes_before_a_newer_single_source_event(conn):
    single = make_event(conn, sources=1, age_days=0.01)
    multi = make_event(conn, sources=2, age_days=0.5)
    ids = queue_ids(conn)
    assert ids.index(multi) < ids.index(single)


def test_among_equals_the_fresher_event_goes_first(conn):
    older, newer = make_event(conn, sources=2, age_days=1.5), make_event(conn, sources=2, age_days=0.2)
    ids = queue_ids(conn)
    assert ids.index(newer) < ids.index(older)


def test_event_older_than_three_days_is_not_in_the_queue(conn):
    assert make_event(conn, sources=2, age_days=3.5) not in queue_ids(conn)
    assert make_event(conn, sources=2, age_days=2.5) in queue_ids(conn)


def test_event_without_claims_is_not_in_the_queue(conn):
    assert make_event(conn, sources=2, claim=False) not in queue_ids(conn)


def test_event_with_a_current_render_leaves_the_queue_and_returns_when_a_source_is_added(conn):
    event_id = make_event(conn, sources=2)
    assert event_id in queue_ids(conn)
    with conn.cursor() as cur:
        cur.execute(
            "insert into renders (event_id, format, lang, body, model) values (%s, 'summary_30s', 'ru', 'текст', 'm')",
            (event_id,),
        )
    db.record_render_result(conn, event_id, ok=True)
    assert event_id not in queue_ids(conn)

    article_id = make_article(conn, make_source(conn), 0.1)
    with conn.cursor() as cur:
        cur.execute("insert into event_articles (article_id, event_id) values (%s, %s)", (article_id, event_id))
    assert event_id in queue_ids(conn)


def test_other_render_format_does_not_count(conn):
    event_id = make_event(conn, sources=2)
    with conn.cursor() as cur:
        cur.execute(
            "insert into renders (event_id, format, lang, body, model) values (%s, 'full_breakdown', 'ru', 'текст', 'm')",
            (event_id,),
        )
    assert event_id in queue_ids(conn)


def test_three_failures_postpone_the_event_for_a_day(conn):
    event_id = make_event(conn, sources=2)
    for _ in range(db.MAX_RENDER_ATTEMPTS - 1):
        db.record_render_result(conn, event_id, ok=False)
    assert event_id in queue_ids(conn)  # две неудачи — ещё в очереди
    db.record_render_result(conn, event_id, ok=False)
    assert event_id not in queue_ids(conn)  # третья — отложено
    set_event(conn, event_id, render_failed_at=None)
    # без метки времени неудачи условие «прошли сутки» не выполняется
    assert event_id not in queue_ids(conn)
    with conn.cursor() as cur:
        cur.execute("update events set render_failed_at = now() - interval '25 hours' where id = %s", (event_id,))
    assert event_id in queue_ids(conn)  # через сутки — снова


def test_success_resets_failure_counter(conn):
    event_id = make_event(conn, sources=2)
    db.record_render_result(conn, event_id, ok=False)
    db.record_render_result(conn, event_id, ok=True)
    with conn.cursor() as cur:
        cur.execute("select render_attempts, render_failed_at, rendered_source_count from events where id = %s", (event_id,))
        row = cur.fetchone()
    assert (row["render_attempts"], row["render_failed_at"], row["rendered_source_count"]) == (0, None, 2)


def test_exclude_removes_events_from_the_queue(conn):
    event_id = make_event(conn, sources=2)
    assert event_id not in queue_ids(conn, exclude=[event_id])


# --- очереди фактов ------------------------------------------------------------


def article_ids(conn, scope):
    return [a["id"] for a in db.articles_missing_claims(conn, 100_000, scope)]


def test_claims_scope_multi_takes_articles_of_fresh_multi_events_even_if_the_article_is_old(conn):
    event_id = make_event(conn, sources=2, age_days=0.1, claim=False)
    old = make_article(conn, make_source(conn), 2.9, claim=False)
    with conn.cursor() as cur:
        cur.execute("insert into event_articles (article_id, event_id) values (%s, %s)", (old, event_id))
        cur.execute("select article_id from event_articles where event_id = %s", (event_id,))
        members = [r["article_id"] for r in cur.fetchall()]
    multi, rest = article_ids(conn, "multi"), article_ids(conn, "rest")
    assert all(a in multi for a in members)
    assert not any(a in rest for a in members)


def test_claims_scope_rest_takes_single_events_and_unclustered_fresh_articles(conn):
    single = make_event(conn, sources=1, claim=False)
    with conn.cursor() as cur:
        cur.execute("select article_id from event_articles where event_id = %s", (single,))
        single_article = cur.fetchone()["article_id"]
    unclustered = make_article(conn, make_source(conn), 0.2, claim=False)
    stale = make_article(conn, make_source(conn), 5, claim=False)
    rest = article_ids(conn, "rest")
    assert single_article in rest and unclustered in rest
    assert stale not in rest and stale not in article_ids(conn, "multi") and stale not in article_ids(conn, "all")


def test_claims_queue_is_freshest_first(conn):
    older = make_article(conn, make_source(conn), 2, claim=False)
    newer = make_article(conn, make_source(conn), 0.1, claim=False)
    ids = article_ids(conn, "rest")
    assert ids.index(newer) < ids.index(older)


def test_poisoned_article_leaves_the_queue_after_three_attempts(conn):
    article_id = make_article(conn, make_source(conn), 0.1, claim=False)
    for _ in range(db.MAX_CLAIMS_ATTEMPTS - 1):
        db.bump_claims_attempts(conn, article_id)
    assert article_id in article_ids(conn, "rest")
    db.bump_claims_attempts(conn, article_id)
    assert article_id not in article_ids(conn, "rest")


def test_embeddings_and_grouping_only_for_fresh_multi_source_events(conn):
    single = make_event(conn, sources=1)
    multi = make_event(conn, sources=2)
    with conn.cursor() as cur:
        cur.execute(
            "select c.id, ea.event_id from claims c join event_articles ea on ea.article_id = c.article_id where ea.event_id = any(%s)",
            ([single, multi],),
        )
        by_event = {}
        for r in cur.fetchall():
            by_event.setdefault(r["event_id"], []).append(r["id"])
    to_embed = {c["id"] for c in db.claims_missing_embedding(conn, 100_000)}
    assert set(by_event[multi]) <= to_embed
    assert not set(by_event[single]) & to_embed
