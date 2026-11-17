"""Построители строк для тестов на настоящей БД (используются внутри откатываемой транзакции)."""

import uuid


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
