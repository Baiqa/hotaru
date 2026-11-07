import logging
from contextlib import contextmanager
from datetime import timedelta

import psycopg
from pgvector.psycopg import register_vector
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)


@contextmanager
def connect(database_url: str):
    conn = psycopg.connect(database_url, autocommit=True, row_factory=dict_row)
    register_vector(conn)
    try:
        yield conn
    finally:
        conn.close()


def seed_sources(conn, sources: list[dict]) -> dict[str, str]:
    """Идемпотентно синхронизирует worker/sources.yaml с таблицей sources.

    sources.yaml — источник правды по списку изданий (не миграция),
    т.к. список публикаций меняется независимо от схемы БД.
    """
    ids: dict[str, str] = {}
    with conn.cursor() as cur:
        for src in sources:
            cur.execute(
                """
                insert into sources (name, base_url, fetch_method, rss_url, lang)
                values (%(name)s, %(base_url)s, %(fetch_method)s, %(rss_url)s, %(lang)s)
                on conflict (base_url) do update set
                    name = excluded.name,
                    fetch_method = excluded.fetch_method,
                    rss_url = excluded.rss_url,
                    lang = excluded.lang
                returning id
                """,
                src,
            )
            ids[src["base_url"]] = cur.fetchone()["id"]
    return ids


def existing_canonical_urls(conn, urls: list[str]) -> set[str]:
    """Какие из этих canonical_url уже есть в БД — чтобы не скачивать их заново
    каждый прогон крона (раз в 30 минут по одному и тому же списку статей)."""
    if not urls:
        return set()
    with conn.cursor() as cur:
        cur.execute("select canonical_url from articles where canonical_url = any(%s)", (urls,))
        return {row["canonical_url"] for row in cur.fetchall()}


def mark_robots_checked(conn, source_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update sources set robots_checked_at = now() where id = %s",
            (source_id,),
        )


def seed_aliases(conn, aliases: list[dict]) -> None:
    """Идемпотентно подгружает aliases_seed.yaml — по образцу seed_sources().

    Каждая запись: canonical_name, type, aliases: [{alias, lang}, ...].
    """
    with conn.cursor() as cur:
        for entry in aliases:
            cur.execute(
                """
                insert into entities (canonical_name, type)
                values (%(canonical_name)s, %(type)s)
                on conflict (canonical_name, type) do update set canonical_name = excluded.canonical_name
                returning id
                """,
                entry,
            )
            entity_id = cur.fetchone()["id"]
            for alias in entry["aliases"]:
                cur.execute(
                    """
                    insert into entity_aliases (entity_id, alias, lang)
                    values (%(entity_id)s, %(alias)s, %(lang)s)
                    on conflict (lower(alias), lang) do nothing
                    """,
                    {"entity_id": entity_id, **alias},
                )


def find_entity_by_alias(conn, alias: str, lang: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "select entity_id from entity_aliases where lower(alias) = lower(%s) and lang = %s",
            (alias, lang),
        )
        row = cur.fetchone()
        return row["entity_id"] if row else None


def list_aliases(conn, lang: str, entity_type: str) -> list[dict]:
    """Кандидаты для нечёткого сопоставления (rapidfuzz) — алиасы того же языка
    и типа сущности, чтобы 'Токаев' (person) не подтягивал 'org'-алиасы."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select ea.alias, ea.entity_id
            from entity_aliases ea
            join entities e on e.id = ea.entity_id
            where ea.lang = %s and e.type = %s
            """,
            (lang, entity_type),
        )
        return cur.fetchall()


def create_entity(conn, canonical_name: str, entity_type: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into entities (canonical_name, type)
            values (%s, %s)
            on conflict (canonical_name, type) do update set canonical_name = excluded.canonical_name
            returning id
            """,
            (canonical_name, entity_type),
        )
        return cur.fetchone()["id"]


def shorten_canonical_name_if_needed(conn, entity_id: str, candidate_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update entities
            set canonical_name = %(candidate_name)s
            where id = %(entity_id)s and length(%(candidate_name)s) < length(canonical_name)
            """,
            {"entity_id": entity_id, "candidate_name": candidate_name},
        )


def add_alias(conn, entity_id: str, alias: str, lang: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into entity_aliases (entity_id, alias, lang)
            values (%s, %s, %s)
            on conflict (lower(alias), lang) do nothing
            """,
            (entity_id, alias, lang),
        )


def upsert_article_entity(conn, article_id: str, entity_id: str, mention_count: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into article_entities (article_id, entity_id, mention_count)
            values (%s, %s, %s)
            on conflict (article_id, entity_id) do update set mention_count = excluded.mention_count
            """,
            (article_id, entity_id, mention_count),
        )


def articles_missing_entities(conn, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select a.id, a.title, a.lead, a.body, s.lang
            from articles a
            join sources s on s.id = a.source_id
            where a.entities_extracted_at is null
            order by a.published_at nulls last
            limit %s
            """,
            (limit,),
        )
        return cur.fetchall()


def mark_entities_extracted(conn, article_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update articles set entities_extracted_at = now() where id = %s",
            (article_id,),
        )


def articles_missing_claims(conn, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, title, lead, body, published_at
            from articles
            where claims_extracted_at is null and fetch_status = 'ok' and body is not null
            order by published_at nulls last
            limit %s
            """,
            (limit,),
        )
        return cur.fetchall()


def insert_claims(conn, article_id: str, claims: list[dict]) -> None:
    """Вставляет claims, выживших после проверки evidence (claim_extraction.py), и их
    числа. Пустой claims — не ошибка (статья без чётких фактов), просто ничего не вставляем."""
    with conn.cursor() as cur:
        for claim in claims:
            cur.execute(
                """
                insert into claims (article_id, text, type, evidence)
                values (%s, %s, %s, %s)
                returning id
                """,
                (article_id, claim["text"], claim["type"], claim["evidence"]),
            )
            claim_id = cur.fetchone()["id"]
            for number in claim["numbers"]:
                cur.execute(
                    """
                    insert into claim_numbers (claim_id, value, unit, label, period)
                    values (%s, %s, %s, %s, %s)
                    """,
                    (claim_id, number["value"], number["unit"], number["label"], number["period"]),
                )


def mark_claims_extracted(conn, article_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update articles set claims_extracted_at = now() where id = %s",
            (article_id,),
        )


def claims_missing_embedding(conn, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "select id, text from claims where embedding is null order by created_at limit %s",
            (limit,),
        )
        return cur.fetchall()


def update_claim_embedding(conn, claim_id: str, vector: list[float]) -> None:
    with conn.cursor() as cur:
        cur.execute("update claims set embedding = %s where id = %s", (vector, claim_id))


def claims_missing_group(conn, limit: int) -> list[dict]:
    """Claims без группы, но только у статей, уже попавших в событие (inner join —
    claim статьи без события сам пока не дошёл до кластеризации, появится в следующем
    прогоне после articles_missing_event, а не застрянет здесь молча)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select c.id, c.article_id, c.text, c.evidence, c.embedding, ea.event_id
            from claims c
            join event_articles ea on ea.article_id = c.article_id
            left join claim_group_members cgm on cgm.claim_id = c.id
            where cgm.claim_id is null and c.embedding is not null
            order by c.created_at
            limit %s
            """,
            (limit,),
        )
        return cur.fetchall()


def event_claim_groups(conn, event_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "select id, bucket, source_count, centroid from claim_groups where event_id = %s",
            (event_id,),
        )
        return cur.fetchall()


def claim_group_representative(conn, claim_group_id: str) -> dict:
    """Текст/evidence самого раннего (founding) члена группы — используется как
    образец для LLM-классификации новой claim против этой группы."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select c.text, c.evidence
            from claim_group_members m
            join claims c on c.id = m.claim_id
            where m.claim_group_id = %s
            order by m.joined_at asc
            limit 1
            """,
            (claim_group_id,),
        )
        return cur.fetchone()


def claim_group_numbers_conflict(conn, claim_group_id: str, claim_id: str) -> bool:
    """Есть ли у нового claim число с тем же label/period, но другим value, чем у
    любого уже состоящего в группе claim — сигнал расхождения даже при высокой
    текстовой близости (напр. разные версии размера ставки)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select exists (
                select 1
                from claim_numbers n1
                join claim_group_members m on m.claim_id = n1.claim_id
                join claim_numbers n2 on n2.claim_id = %(claim_id)s
                where m.claim_group_id = %(group_id)s
                  and n1.label is not distinct from n2.label
                  and n1.period is not distinct from n2.period
                  and n1.value != n2.value
            ) as conflict
            """,
            {"claim_id": claim_id, "group_id": claim_group_id},
        )
        return cur.fetchone()["conflict"]


def create_claim_group(conn, event_id: str, claim_id: str, embedding) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into claim_groups (event_id, bucket, source_count, centroid)
            values (%s, 'unique', 1, %s)
            returning id
            """,
            (event_id, embedding),
        )
        group_id = cur.fetchone()["id"]
        cur.execute(
            """
            insert into claim_group_members (claim_id, claim_group_id, relation, similarity_score)
            values (%s, %s, 'confirms', null)
            """,
            (claim_id, group_id),
        )
    return group_id


def add_claim_group_member(conn, claim_group_id: str, claim_id: str, relation: str, similarity_score: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into claim_group_members (claim_id, claim_group_id, relation, similarity_score)
            values (%s, %s, %s, %s)
            """,
            (claim_id, claim_group_id, relation, similarity_score),
        )


def recompute_claim_group(conn, claim_group_id: str) -> None:
    """Полный пересчёт centroid/source_count/bucket — группы маленькие, дёшево.
    bucket: disputed, если есть хоть один relation='contradicts'; иначе confirmed при
    source_count>=2 (считаем по изданиям, не по статьям — см. tmp/plans); иначе unique."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select c.embedding
            from claim_group_members m
            join claims c on c.id = m.claim_id
            where m.claim_group_id = %s and c.embedding is not null
            """,
            (claim_group_id,),
        )
        vectors = [row["embedding"].to_list() for row in cur.fetchall()]
        centroid = [sum(coords) / len(vectors) for coords in zip(*vectors)] if vectors else None

        cur.execute(
            """
            select count(distinct a.source_id) as source_count
            from claim_group_members m
            join claims c on c.id = m.claim_id
            join articles a on a.id = c.article_id
            where m.claim_group_id = %s
            """,
            (claim_group_id,),
        )
        source_count = cur.fetchone()["source_count"]

        cur.execute(
            "select exists(select 1 from claim_group_members where claim_group_id = %s and relation = 'contradicts') as has_conflict",
            (claim_group_id,),
        )
        has_conflict = cur.fetchone()["has_conflict"]

        bucket = "disputed" if has_conflict else ("confirmed" if source_count >= 2 else "unique")

        cur.execute(
            "update claim_groups set centroid = %s, source_count = %s, bucket = %s where id = %s",
            (centroid, source_count, bucket, claim_group_id),
        )


def articles_missing_embedding(conn, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, title, lead
            from articles
            where embedding is null
            order by published_at nulls last
            limit %s
            """,
            (limit,),
        )
        return cur.fetchall()


def update_embedding(conn, article_id: str, vector: list[float]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update articles set embedding = %s where id = %s",
            (vector, article_id),
        )


def get_article(conn, article_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "select id, title, lead, body, published_at, embedding from articles where id = %s",
            (article_id,),
        )
        return cur.fetchone()


def entity_ids_by_article(conn, article_ids: list[str]) -> dict[str, set[str]]:
    """Множества entity_id для нескольких статей за один запрос (вместо N+1)."""
    result: dict[str, set[str]] = {aid: set() for aid in article_ids}
    if not article_ids:
        return result
    with conn.cursor() as cur:
        cur.execute(
            "select article_id, entity_id from article_entities where article_id = any(%s)",
            (article_ids,),
        )
        for row in cur.fetchall():
            result[row["article_id"]].add(row["entity_id"])
    return result


def find_candidate_articles(
    conn,
    article_id: str,
    embedding,
    entity_ids: list[str],
    window_days: int | None,
    published_at,
    limit: int,
) -> list[dict]:
    """Раздел 3.3 CLAUDE.md: кандидаты — статьи, делящие хотя бы одну сущность,
    в пределах окна по времени, топ-N по косинусной близости pgvector. DISTINCT
    через EXISTS (не JOIN), чтобы статья, делящая несколько сущностей с целевой,
    не размножалась на несколько строк.
    """
    params: dict = {
        "article_id": article_id,
        "embedding": embedding,
        "entity_ids": entity_ids,
        "limit": limit,
    }
    time_filter = ""
    if window_days is not None and published_at is not None:
        time_filter = "and a.published_at between %(from)s and %(to)s"
        params["from"] = published_at - timedelta(days=window_days)
        params["to"] = published_at + timedelta(days=window_days)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select a.id, a.title, a.lead, a.body, a.published_at,
                   1 - (a.embedding <=> %(embedding)s) as cosine_sim
            from articles a
            where a.id != %(article_id)s
              and a.embedding is not null
              and exists (
                select 1 from article_entities ae
                where ae.article_id = a.id and ae.entity_id = any(%(entity_ids)s)
              )
              {time_filter}
            order by a.embedding <=> %(embedding)s
            limit %(limit)s
            """,
            params,
        )
        return cur.fetchall()


def list_active_events(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select e.id, e.title, e.centroid, e.started_at,
                   (select count(*) from event_articles ea where ea.event_id = e.id) as article_count
            from events e
            where e.status = 'active' and e.centroid is not null
            """
        )
        return cur.fetchall()


def event_entity_ids(conn, event_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct ae.entity_id
            from event_articles ea
            join article_entities ae on ae.article_id = ea.article_id
            where ea.event_id = %s
            """,
            (event_id,),
        )
        return [row["entity_id"] for row in cur.fetchall()]


def event_majority_entities(conn, event_id: str) -> set[str]:
    """Сущности, встречающиеся в >=половине статей события — сигнал, которого не хватает
    чистому косинусу центроидов в event_merge.py (см. tmp/plans/etap1-kalibrovka-po-keisam.md,
    находка 1): две "одножанровые", но реально разных события подборки статей могут иметь
    близкий косинус, но не делят ни одной сущности, присутствующей в большинстве статей."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select ae.entity_id
            from event_articles ea
            join article_entities ae on ae.article_id = ea.article_id
            where ea.event_id = %(event_id)s
            group by ae.entity_id
            having count(distinct ea.article_id) * 2 >= (
                select count(*) from event_articles where event_id = %(event_id)s
            )
            """,
            {"event_id": event_id},
        )
        return {row["entity_id"] for row in cur.fetchall()}


def find_similar_events(
    conn,
    event_id: str,
    centroid,
    entity_ids: list[str],
    window_days: int,
    started_at,
    limit: int,
) -> list[dict]:
    """По образцу find_candidate_articles, но на уровне событий: общая сущность
    (через их статьи), окно по started_at, топ-N по косинусу центроидов."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select e.id, e.started_at,
                   (select count(*) from event_articles ea2 where ea2.event_id = e.id) as article_count,
                   1 - (e.centroid <=> %(centroid)s) as cosine_sim
            from events e
            where e.id != %(event_id)s
              and e.status = 'active'
              and e.centroid is not null
              and e.started_at between %(from)s and %(to)s
              and exists (
                select 1 from event_articles ea
                join article_entities ae on ae.article_id = ea.article_id
                where ea.event_id = e.id and ae.entity_id = any(%(entity_ids)s)
              )
            order by e.centroid <=> %(centroid)s
            limit %(limit)s
            """,
            {
                "event_id": event_id,
                "centroid": centroid,
                "entity_ids": entity_ids,
                "from": started_at - timedelta(days=window_days),
                "to": started_at + timedelta(days=window_days),
                "limit": limit,
            },
        )
        return cur.fetchall()


def merge_events(conn, canonical_id: str, absorbed_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update event_articles set event_id = %s where event_id = %s",
            (canonical_id, absorbed_id),
        )
        cur.execute(
            "update events set canonical_id = %s, status = 'merged' where id = %s",
            (canonical_id, absorbed_id),
        )
        cur.execute(
            """
            update events set started_at = least(started_at, (select started_at from events where id = %s))
            where id = %s
            """,
            (absorbed_id, canonical_id),
        )


def event_member_embeddings(conn, event_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select a.id as article_id, a.embedding, a.published_at, a.title
            from articles a
            join event_articles ea on ea.article_id = a.id
            where ea.event_id = %s
            """,
            (event_id,),
        )
        return cur.fetchall()


def create_event_from_articles(conn, title: str, started_at, article_ids: list[str]) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "insert into events (title, started_at) values (%s, %s) returning id",
            (title, started_at),
        )
        new_event_id = cur.fetchone()["id"]
        cur.execute(
            "update event_articles set event_id = %s where article_id = any(%s)",
            (new_event_id, article_ids),
        )
    return new_event_id


def articles_missing_event(conn, limit: int) -> list[dict]:
    """Раздел 3.3 CLAUDE.md — алгоритм инкрементальный, порядок обработки важен
    (кандидат должен успеть получить событие до того, как его сравнят со следующей
    статьёй), поэтому строго по published_at, не просто "необработанные"."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select a.id, a.title, a.lead, a.published_at, a.embedding
            from articles a
            where a.embedding is not null
              and a.entities_extracted_at is not null
              and not exists (select 1 from event_articles ea where ea.article_id = a.id)
            order by a.published_at nulls last
            limit %s
            """,
            (limit,),
        )
        return cur.fetchall()


def event_id_for_article(conn, article_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("select event_id from event_articles where article_id = %s", (article_id,))
        row = cur.fetchone()
        return row["event_id"] if row else None


def create_event(conn, article: dict) -> str:
    """Заводит новое событие со статьёй-основателем. title/started_at/centroid
    берутся из неё; полноценный синтез title — Этап 2, тут временная заглушка."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into events (title, started_at, centroid)
            values (%(title)s, coalesce(%(published_at)s, now()), %(embedding)s)
            returning id
            """,
            article,
        )
        event_id = cur.fetchone()["id"]
        cur.execute(
            """
            insert into event_articles (article_id, event_id, similarity_score)
            values (%s, %s, null)
            """,
            (article["id"], event_id),
        )
    return event_id


def join_event(conn, event_id: str, article_id: str, similarity_score: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into event_articles (article_id, event_id, similarity_score)
            values (%s, %s, %s)
            """,
            (article_id, event_id, similarity_score),
        )


def recompute_event_centroid(conn, event_id: str) -> None:
    """Полный пересчёт (не инкрементальный) — при текущих объёмах дёшево, а раздел
    3.3 явно требует НЕ использовать centroid для решений о совпадении (только для
    показа/будущего merge), так что цена малой неточности здесь низкая."""
    with conn.cursor() as cur:
        cur.execute(
            "select a.embedding from articles a join event_articles ea on ea.article_id = a.id where ea.event_id = %s",
            (event_id,),
        )
        vectors = [row["embedding"].to_list() for row in cur.fetchall()]
        if not vectors:
            return
        centroid = [sum(coords) / len(vectors) for coords in zip(*vectors)]
        cur.execute("update events set centroid = %s where id = %s", (centroid, event_id))


def get_gray_zone_verdict(conn, article_a_id: str, article_b_id: str) -> str | None:
    a, b = sorted((article_a_id, article_b_id))
    with conn.cursor() as cur:
        cur.execute(
            "select verdict from gray_zone_verdicts where article_a_id = %s and article_b_id = %s",
            (a, b),
        )
        row = cur.fetchone()
        return row["verdict"] if row else None


def save_gray_zone_verdict(conn, article_a_id: str, article_b_id: str, verdict: str) -> None:
    a, b = sorted((article_a_id, article_b_id))
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into gray_zone_verdicts (article_a_id, article_b_id, verdict)
            values (%s, %s, %s)
            on conflict (article_a_id, article_b_id) do nothing
            """,
            (a, b, verdict),
        )


def articles_for_events(conn, event_ids: list[str]) -> list[dict]:
    """Полные данные статей для набора событий — для экспорта в инструмент
    разметки B-Cubed (worker/eval/sample.py)."""
    if not event_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            select a.id as article_id, a.title, a.lead, a.published_at,
                   ea.event_id, s.name as source_name
            from event_articles ea
            join articles a on a.id = ea.article_id
            join sources s on s.id = a.source_id
            where ea.event_id = any(%s)
            """,
            (event_ids,),
        )
        return cur.fetchall()


def event_ids_for_articles(conn, article_ids: list[str]) -> dict[str, str]:
    """article_id -> текущий event_id — для сверки с ручной разметкой
    (worker/eval/bcubed.py). Читает live-состояние, а не экспортированный
    снимок, т.к. merge/split могли отработать между разметкой и подсчётом."""
    if not article_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "select article_id, event_id from event_articles where article_id = any(%s)",
            (article_ids,),
        )
        return {str(row["article_id"]): str(row["event_id"]) for row in cur.fetchall()}


def insert_article(conn, article: dict) -> str:
    """Вставляет статью. Дедуп по canonical_url/content_hash обеспечивают
    unique-constraint'ы миграции — при конфликте просто считаем дублем."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into articles
                    (source_id, url, canonical_url, title, lead, body, published_at,
                     content_hash, fetch_status)
                values
                    (%(source_id)s, %(url)s, %(canonical_url)s, %(title)s, %(lead)s, %(body)s,
                     %(published_at)s, %(content_hash)s, %(fetch_status)s)
                """,
                article,
            )
        return "inserted"
    except pg_errors.UniqueViolation:
        return "duplicate"


def save_monitoring_check(conn, check_name: str, status: str, details: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "insert into monitoring_checks (check_name, status, details) values (%s, %s, %s)",
            (check_name, status, Jsonb(details)),
        )


def events_missing_render(conn, limit: int) -> list[dict]:
    """События без summary_30s render, у которых есть хотя бы один claim — нет
    смысла синтезировать текст для события без фактов. Не ждёт завершения
    embedding/группировки claims (раздел 3.4: синтез работает по фактам события,
    группировка только уточняет bucket — см. event_facts_for_synthesis)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select e.id, e.title
            from events e
            where e.status = 'active'
              and exists (
                select 1 from event_articles ea join claims c on c.article_id = ea.article_id
                where ea.event_id = e.id
              )
              and (
                select count(*) from renders r where r.event_id = e.id
              ) < 2
            order by e.started_at
            limit %s
            """,
            (limit,),
        )
        return cur.fetchall()


def event_facts_for_synthesis(conn, event_id: str) -> list[dict]:
    """Факты события для синтеза (раздел 3.4 CLAUDE.md) — модель-синтезатор видит
    ТОЛЬКО это, не исходные статьи. bucket по умолчанию 'unique', если claim ещё не
    дошёл до группировки (claims_missing_group работает в фоне отдельно) — так
    синтез не блокируется незавершённой группировкой, просто консервативно считает
    факт пока неподтверждённым."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select c.id as claim_id, c.text, c.evidence, c.type,
                   s.name as source_name, a.url as source_url,
                   coalesce(cg.bucket, 'unique') as bucket,
                   coalesce(cg.source_count, 1) as source_count
            from claims c
            join articles a on a.id = c.article_id
            join sources s on s.id = a.source_id
            join event_articles ea on ea.article_id = c.article_id
            left join claim_group_members cgm on cgm.claim_id = c.id
            left join claim_groups cg on cg.id = cgm.claim_group_id
            where ea.event_id = %s
            order by c.created_at
            """,
            (event_id,),
        )
        claims = cur.fetchall()
        if not claims:
            return []

        claim_ids = [row["claim_id"] for row in claims]
        cur.execute(
            "select claim_id, value, unit, label, period from claim_numbers where claim_id = any(%s)",
            (claim_ids,),
        )
        numbers_by_claim: dict[str, list[dict]] = {}
        for row in cur.fetchall():
            numbers_by_claim.setdefault(row["claim_id"], []).append(
                {"value": float(row["value"]), "unit": row["unit"], "label": row["label"], "period": row["period"]}
            )
        for row in claims:
            row["numbers"] = numbers_by_claim.get(row["claim_id"], [])
        return claims


def save_render(
    conn, event_id: str, format: str, lang: str, body: str, model: str, sentence_claims: list[list[str]]
) -> None:
    """Апсерт render + полная замена его render_claims — безопасно для повторного
    запуска (не копит дубликаты предложений при перегенерации)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into renders (event_id, format, lang, body, model)
            values (%s, %s, %s, %s, %s)
            on conflict (event_id, format, lang)
            do update set body = excluded.body, model = excluded.model, updated_at = now()
            returning id
            """,
            (event_id, format, lang, body, model),
        )
        render_id = cur.fetchone()["id"]
        cur.execute("delete from render_claims where render_id = %s", (render_id,))
        for sentence_index, claim_ids in enumerate(sentence_claims):
            for claim_id in claim_ids:
                cur.execute(
                    "insert into render_claims (render_id, sentence_index, claim_id) values (%s, %s, %s)",
                    (render_id, sentence_index, claim_id),
                )


def upsert_metric_series(conn, key: str, label: str, unit: str, source_name: str, source_url: str) -> str:
    """Этап 3 MVP: ключ 'indicator:country' уникален, повторный запуск обновляет
    метаданные на месте (на случай смены подписи/ссылки), не плодя дубликаты."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into metric_series (key, label, unit, source_name, source_url)
            values (%s, %s, %s, %s, %s)
            on conflict (key) do update set
                label = excluded.label, unit = excluded.unit,
                source_name = excluded.source_name, source_url = excluded.source_url,
                updated_at = now()
            returning id
            """,
            (key, label, unit, source_name, source_url),
        )
        return cur.fetchone()["id"]


def upsert_metric_points(conn, series_id: str, points: list[dict]) -> None:
    with conn.cursor() as cur:
        for point in points:
            cur.execute(
                """
                insert into metric_points (series_id, period, value)
                values (%s, %s, %s)
                on conflict (series_id, period) do update set value = excluded.value
                """,
                (series_id, point["period"], point["value"]),
            )


def events_missing_story(conn) -> list[dict]:
    """Этап 4: активные события с центроидом, ещё не привязанные ни к одному
    сюжету (story_events.event_id — PK, один event принадлежит ровно одному
    story, по аналогии с event_articles.article_id)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select e.id, e.title, e.centroid, e.started_at
            from events e
            where e.status = 'active'
              and e.centroid is not null
              and not exists (select 1 from story_events se where se.event_id = e.id)
            order by e.started_at
            """
        )
        return cur.fetchall()


def find_similar_stories(
    conn,
    centroid,
    entity_ids: list[str],
    window_days: int,
    started_at,
    limit: int,
    recent_k: int,
    min_sharing: int,
    max_story_size: int,
) -> list[dict]:
    """Кандидаты-сюжеты для события. Сравнение идёт не с центроидом сюжета, а с
    последними `recent_k` участниками (CLAUDE.md §3.3, «дрейф»): центроид сюжета
    усредняет всё присоединённое и дрейфует к «средней новости», после чего к
    нему проходит почти любое событие (случай 2026-11-07: сюжеты на 376/295
    событий, см. tmp/plans/etap4-ispravlenie-syuzhetov.md).

    cosine_sim — максимум по последним участникам. Фильтры: окно по времени
    относительно самого свежего из них; общая сущность хотя бы у `min_sharing`
    из последних участников (меньше, если их в сюжете меньше); потолок размера
    сюжета как страховка от цепного разрастания."""
    with conn.cursor() as cur:
        cur.execute(
            """
            with recent as (
              select se.story_id, se.event_id, e.centroid, e.started_at,
                     row_number() over (partition by se.story_id order by e.started_at desc) as rn,
                     count(*) over (partition by se.story_id) as story_size
              from story_events se
              join events e on e.id = se.event_id
              join stories s on s.id = se.story_id
              where s.status = 'active' and e.centroid is not null
            ),
            scored as (
              select r.story_id, r.story_size, r.started_at,
                     1 - (r.centroid <=> %(centroid)s) as sim,
                     exists (
                       select 1 from event_articles ea
                       join article_entities ae on ae.article_id = ea.article_id
                       where ea.event_id = r.event_id and ae.entity_id = any(%(entity_ids)s)
                     ) as shares
              from recent r
              where r.rn <= %(recent_k)s
            )
            select story_id as id,
                   max(sim) as cosine_sim,
                   max(story_size) as story_size
            from scored
            group by story_id
            having max(started_at) between %(from)s and %(to)s
               and max(story_size) < %(max_story_size)s
               and count(*) filter (where shares) >= least(%(min_sharing)s, count(*))
            order by max(sim) desc
            limit %(limit)s
            """,
            {
                "centroid": centroid,
                "entity_ids": entity_ids,
                "from": started_at - timedelta(days=window_days),
                "to": started_at + timedelta(days=window_days),
                "limit": limit,
                "recent_k": recent_k,
                "min_sharing": min_sharing,
                "max_story_size": max_story_size,
            },
        )
        return cur.fetchall()


def create_story(conn, event: dict) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into stories (title, started_at, centroid)
            values (%(title)s, %(started_at)s, %(centroid)s)
            returning id
            """,
            event,
        )
        story_id = cur.fetchone()["id"]
        cur.execute(
            "insert into story_events (event_id, story_id, position) values (%s, %s, 1)",
            (event["id"], story_id),
        )
        return story_id


def assign_event_to_story(conn, event_id: str, story_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "insert into story_events (event_id, story_id, position) values (%s, %s, 0)",
            (event_id, story_id),
        )
    # position=0 выше временный — recompute_story_positions сразу расставляет
    # по started_at, как recompute_event_centroid делает полный, не
    # инкрементальный пересчёт у событий.
    recompute_story_centroid(conn, story_id)
    recompute_story_positions(conn, story_id)


def recompute_story_centroid(conn, story_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "select e.centroid from events e join story_events se on se.event_id = e.id where se.story_id = %s",
            (story_id,),
        )
        vectors = [row["centroid"].to_list() for row in cur.fetchall()]
        if not vectors:
            return
        centroid = [sum(coords) / len(vectors) for coords in zip(*vectors)]
        cur.execute("update stories set centroid = %s, updated_at = now() where id = %s", (centroid, story_id))


def recompute_story_positions(conn, story_id: str) -> None:
    # Один UPDATE вместо N отдельных (по мере роста сюжета N запросов по сети
    # замедляли прогон: ~1 событие в несколько секунд на сюжетах из сотен).
    with conn.cursor() as cur:
        cur.execute(
            """
            update story_events se
            set position = r.pos
            from (
              select se2.event_id,
                     row_number() over (order by e.started_at, se2.event_id) as pos
              from story_events se2
              join events e on e.id = se2.event_id
              where se2.story_id = %(story_id)s
            ) r
            where se.event_id = r.event_id and se.story_id = %(story_id)s
            """,
            {"story_id": story_id},
        )
