"""Раздел 6 CLAUDE.md: "проверки, которые должны работать постоянно". Каждая функция —
чистая логика (без записи в БД, этим занимается monitor.py), возвращает структуру с
status: 'ok' | 'alert' | 'no_data' — 'no_data' там, где проверка технически не может
сработать (нет данных за окно, этап пайплайна ещё не реализован), это НЕ alert.
"""

TEXT_LENGTH_DROP_RATIO = 0.5  # "упала более чем вдвое"
RENDER_REJECTION_THRESHOLD = 0.10
GRAY_ZONE_SHARE_THRESHOLD = 0.25


def check_silent_sources(conn, window_hours: int = 24) -> list[dict]:
    """"источник за 24 часа дал 0 статей". Считаем по articles.created_at (когда МЫ
    собрали статью), а не published_at (когда её выпустило издание) — иначе проверка
    мерила бы редакционную активность источника, а не здоровье парсера."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select s.id as source_id, s.name as source_name,
                   count(a.id) filter (
                     where a.created_at >= now() - (%(h)s || ' hours')::interval
                   ) as recent_count
            from sources s
            left join articles a on a.source_id = s.id
            group by s.id, s.name
            order by s.name
            """,
            {"h": window_hours},
        )
        rows = cur.fetchall()
    results = []
    for row in rows:
        status = "alert" if row["recent_count"] == 0 else "ok"
        results.append(
            {
                # str(): psycopg отдаёт uuid.UUID, Jsonb/json.dumps его не сериализует.
                "source_id": str(row["source_id"]),
                "source_name": row["source_name"],
                "recent_count": row["recent_count"],
                "window_hours": window_hours,
                "status": status,
            }
        )
    return results


def check_text_length_drop(conn, window_hours: int = 24) -> list[dict]:
    """"средняя длина текста по источнику упала более чем вдвое". Сравниваем среднюю
    длину body статей за последнее окно с историческим средним того же источника (вне
    окна). Без данных в любой из двух корзин — status='no_data', не ложный alert."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                s.id as source_id, s.name as source_name,
                avg(length(a.body)) filter (
                    where a.created_at >= now() - (%(h)s || ' hours')::interval and a.body is not null
                ) as recent_avg,
                count(a.id) filter (
                    where a.created_at >= now() - (%(h)s || ' hours')::interval and a.body is not null
                ) as recent_count,
                avg(length(a.body)) filter (
                    where a.created_at < now() - (%(h)s || ' hours')::interval and a.body is not null
                ) as baseline_avg,
                count(a.id) filter (
                    where a.created_at < now() - (%(h)s || ' hours')::interval and a.body is not null
                ) as baseline_count
            from sources s
            left join articles a on a.source_id = s.id
            group by s.id, s.name
            order by s.name
            """,
            {"h": window_hours},
        )
        rows = cur.fetchall()
    results = []
    for row in rows:
        if not row["recent_count"] or not row["baseline_count"]:
            status = "no_data"
        elif row["recent_avg"] < row["baseline_avg"] * TEXT_LENGTH_DROP_RATIO:
            status = "alert"
        else:
            status = "ok"
        results.append(
            {
                "source_id": str(row["source_id"]),
                "source_name": row["source_name"],
                "recent_avg_length": float(row["recent_avg"]) if row["recent_avg"] is not None else None,
                "baseline_avg_length": float(row["baseline_avg"]) if row["baseline_avg"] is not None else None,
                "status": status,
            }
        )
    return results


def check_render_rejection_rate(conn) -> dict:
    """"доля отклонённых генераций выше 10%". Раздел 3.4 CLAUDE.md: отклонённая
    генерация ("вся генерация отклоняется, повтор") нигде не персистится как таковая —
    ни таблицы renders (раздел 4), ни счётчика попыток пока не существует (этап синтеза
    текста не реализован). Пока этого нет — status='no_data', а не alert."""
    with conn.cursor() as cur:
        cur.execute("select to_regclass('public.renders') as renders_table")
        exists = cur.fetchone()["renders_table"] is not None
    if not exists:
        return {
            "status": "no_data",
            "reason": "таблица renders ещё не создана — этап синтеза текста не реализован",
        }
    # TODO: когда появится renders + учёт отклонённых попыток генерации (этап "Пост-
    # проверка" из раздела 5 Этапа 2) — считать долю здесь.
    return {"status": "no_data", "reason": "учёт отклонённых генераций пока не реализован"}


def check_gray_zone_share(conn) -> dict:
    """"доля серой зоны в кластеризации выше 25% → пороги съехали".

    ПРИБЛИЖЕНИЕ, не точная метрика: gray_zone_verdicts хранит пары статей без роли
    (кто был "целью", кто "кандидатом"), так что точную долю "решений о кластеризации,
    которым потребовалась LLM" из текущей схемы восстановить нельзя (для этого нужен
    был бы отдельный лог по каждой целевой статье). Берём долю статей, затронутых хотя
    бы одним gray-zone сравнением с любой стороны пары, от всех статей, прошедших
    кластеризацию (event_articles) — честная оценка нагрузки на серую зону, но не то
    же самое, что "доля решений"."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(distinct aid) as touched from (
                select article_a_id as aid from gray_zone_verdicts
                union
                select article_b_id from gray_zone_verdicts
            ) t
            """
        )
        touched = cur.fetchone()["touched"]
        cur.execute("select count(*) as total from event_articles")
        total = cur.fetchone()["total"]
    if not total:
        return {"status": "no_data", "reason": "кластеризация ещё не запускалась"}
    share = touched / total
    status = "alert" if share > GRAY_ZONE_SHARE_THRESHOLD else "ok"
    return {
        "touched_articles": touched,
        "total_articles": total,
        "share": share,
        "status": status,
        "note": "приближение: доля статей, затронутых хотя бы одним gray-zone сравнением с любой стороны пары",
    }
