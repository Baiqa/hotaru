"""Фаза 6 плана P0: выбор и удаление затрагиваемых пересборкой данных — на настоящей БД
в откатываемой транзакции. Все созданные строки синтетические; реальные события не трогаются:
окно days=0 ничего не выбирает, события отбираются по явному списку статей (как размеченные)."""

import uuid

from dbfixtures import make_article, make_event, make_source
from hotaru_worker import rebuild


def articles_of(conn, event_id) -> list:
    with conn.cursor() as cur:
        cur.execute("select article_id from event_articles where event_id = %s", (event_id,))
        return [r["article_id"] for r in cur.fetchall()]


def scope_for(conn, *article_ids, days=0):
    return rebuild.select_scope(conn, days, [str(a) for a in article_ids])


def merged_pointer(conn, canonical_id) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "insert into events (title, started_at, status, canonical_id) values ('слито', now(), 'merged', %s) returning id",
            (canonical_id,),
        )
        return cur.fetchone()["id"]


def make_entity(conn, name="Тестовая сущность", entity_type="org") -> str:
    with conn.cursor() as cur:
        cur.execute(
            "insert into entities (canonical_name, type) values (%s, %s) returning id",
            (f"{name} {uuid.uuid4().hex[:6]}", entity_type),
        )
        return cur.fetchone()["id"]


def link(conn, article_id, entity_id):
    with conn.cursor() as cur:
        cur.execute(
            "insert into article_entities (article_id, entity_id, mention_count) values (%s, %s, 1)",
            (article_id, entity_id),
        )


# --- выбор ----------------------------------------------------------------------


def test_window_selects_events_with_a_fresh_article_only(conn):
    fresh, old = make_event(conn, age_days=1), make_event(conn, age_days=10)
    scope = rebuild.select_scope(conn, 7, [])
    assert fresh in scope["events"] and old not in scope["events"]


def test_mixed_event_is_taken_whole_with_its_old_articles(conn):
    event_id = make_event(conn, sources=1, age_days=1)
    old_article = make_article(conn, make_source(conn), 12)
    with conn.cursor() as cur:
        cur.execute("insert into event_articles (article_id, event_id) values (%s, %s)", (old_article, event_id))
    scope = rebuild.select_scope(conn, 7, [])
    assert event_id in scope["events"]
    # старая статья тоже потеряет событие и пройдёт кластеризацию заново
    assert old_article in scope["articles"]
    assert scope["window_articles"] >= 1


def test_merged_pointer_to_a_selected_event_is_selected_transitively(conn):
    target = make_event(conn)
    first = merged_pointer(conn, target)
    second = merged_pointer(conn, first)  # цепочка слияний
    scope = scope_for(conn, articles_of(conn, target)[0])
    assert {target, first, second} <= set(scope["events"])
    assert scope["merged_pointers"] >= 2


def test_event_outside_the_scope_is_left_alone(conn):
    chosen, other = make_event(conn), make_event(conn)
    scope = scope_for(conn, articles_of(conn, chosen)[0])
    assert chosen in scope["events"] and other not in scope["events"]


# --- удаление -------------------------------------------------------------------


def test_delete_removes_events_pointers_and_cascades_but_keeps_articles_and_claims(conn):
    event_id = make_event(conn, sources=2)
    pointer = merged_pointer(conn, event_id)
    articles = articles_of(conn, event_id)
    with conn.cursor() as cur:
        cur.execute(
            "insert into renders (event_id, format, lang, body, model) values (%s, 'summary_30s', 'ru', 'т', 'm')",
            (event_id,),
        )
        cur.execute("insert into claim_groups (event_id, bucket, source_count) values (%s, 'unique', 1)", (event_id,))
    scope = scope_for(conn, articles[0])
    stats = rebuild.delete_scope(conn, scope, [], commit=False)

    with conn.cursor() as cur:
        cur.execute("select count(*) n from events where id = any(%s)", ([event_id, pointer],))
        assert cur.fetchone()["n"] == 0
        for table in ("event_articles", "renders", "claim_groups"):
            cur.execute(f"select count(*) n from {table} where event_id = %s", (event_id,))
            assert cur.fetchone()["n"] == 0, table
        cur.execute("select count(*) n from articles where id = any(%s)", (articles,))
        assert cur.fetchone()["n"] == 2
        cur.execute("select count(*) n from claims where article_id = any(%s)", (articles,))
        assert cur.fetchone()["n"] == 2
    assert stats["events_deleted"] >= 1 and stats["merged_deleted"] >= 1


def test_delete_resets_entities_of_the_scope_articles(conn):
    event_id = make_event(conn, sources=1)
    (article,) = articles_of(conn, event_id)
    link(conn, article, make_entity(conn))
    with conn.cursor() as cur:
        cur.execute("update articles set entities_extracted_at = now() where id = %s", (article,))
    rebuild.delete_scope(conn, scope_for(conn, article), [], commit=False)
    with conn.cursor() as cur:
        cur.execute("select entities_extracted_at from articles where id = %s", (article,))
        assert cur.fetchone()["entities_extracted_at"] is None
        cur.execute("select count(*) n from article_entities where article_id = %s", (article,))
        assert cur.fetchone()["n"] == 0


def test_orphan_entities_are_deleted_but_shared_and_seeded_ones_stay(conn):
    event_id = make_event(conn, sources=1)
    (article,) = articles_of(conn, event_id)
    outside = make_article(conn, make_source(conn), 30)  # статья вне пересборки
    orphan, shared, seeded = make_entity(conn), make_entity(conn), make_entity(conn, "Посевная")
    link(conn, article, orphan)
    link(conn, article, shared)
    link(conn, outside, shared)
    with conn.cursor() as cur:
        cur.execute("select canonical_name from entities where id = %s", (seeded,))
        seeded_name = cur.fetchone()["canonical_name"]
    rebuild.delete_scope(conn, scope_for(conn, article), [seeded_name], commit=False)
    with conn.cursor() as cur:
        cur.execute("select id from entities where id = any(%s)", ([orphan, shared, seeded],))
        left = {r["id"] for r in cur.fetchall()}
    assert left == {shared, seeded}


def test_articles_outside_the_scope_keep_entities_and_events(conn):
    inside, outside = make_event(conn), make_event(conn)
    (outside_article,) = articles_of(conn, outside)
    link(conn, outside_article, make_entity(conn))
    with conn.cursor() as cur:
        cur.execute("update articles set entities_extracted_at = now() where id = %s", (outside_article,))
    rebuild.delete_scope(conn, scope_for(conn, articles_of(conn, inside)[0]), [], commit=False)
    with conn.cursor() as cur:
        cur.execute("select count(*) n from events where id = %s", (outside,))
        assert cur.fetchone()["n"] == 1
        cur.execute("select count(*) n from article_entities where article_id = %s", (outside_article,))
        assert cur.fetchone()["n"] == 1
        cur.execute("select entities_extracted_at from articles where id = %s", (outside_article,))
        assert cur.fetchone()["entities_extracted_at"] is not None


def test_stories_are_deleted_with_their_links_but_not_events(conn):
    event_id = make_event(conn)
    with conn.cursor() as cur:
        cur.execute("insert into stories (title, started_at) values ('сюжет', now()) returning id")
        story = cur.fetchone()["id"]
        cur.execute("insert into story_events (story_id, event_id, position) values (%s, %s, 0)", (story, event_id))
    stats = rebuild.delete_stories(conn)
    assert stats["stories_deleted"] >= 1 and stats["story_events_deleted"] >= 1
    with conn.cursor() as cur:
        cur.execute("select count(*) n from story_events where story_id = %s", (story,))
        assert cur.fetchone()["n"] == 0
        cur.execute("select count(*) n from events where id = %s", (event_id,))
        assert cur.fetchone()["n"] == 1
