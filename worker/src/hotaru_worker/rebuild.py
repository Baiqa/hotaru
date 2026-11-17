"""Пересборка событий за окно (фаза 6 плана P0, tmp/plans/p0-ispravleniya-posle-audita.md).

ЭТО УДАЛЕНИЕ ДАННЫХ. Команды:

  plan      только считает, что будет затронуто (БД не меняется);
  backup    полная копия затрагиваемых таблиц в CSV (БД не меняется);
  delete    удаляет события окна, сбрасывает сущности их статей (требует --yes);
  stories   удаляет все сюжеты, чтобы assign-stories собрал их заново (требует --yes).

Что такое «окно»: события, в которых есть хотя бы одна статья моложе --days суток, и (с
--labeled) события, где лежат размеченные для B-Cubed статьи (eval/labels.json). Событие
пересобирается целиком: статьи вне окна, попавшие в такое событие, тоже теряют его и
кластеризуются заново — иначе старая статья осталась бы без события или в чужом.

Сохраняются: статьи и их тексты, извлечённые факты (claims), кеш вердиктов серой зоны,
эмбеддинги. Удаляются каскадом: event_articles, claim_groups (+члены), renders (+render_claims),
story_events. Слитые события, указывающие на удалённые (events.canonical_id), удаляются тоже:
иначе внешний ключ не даст удалить цель.

Дальше конвейер идёт обычным порядком: extract-entities (сущности сброшены), cluster-events,
merge-events, assign-stories, extract-claims/group-claims/synthesize.
"""

import argparse
import csv
import json
import logging
from datetime import datetime
from pathlib import Path

import yaml

from . import config, db

logger = logging.getLogger("hotaru_worker.rebuild")

EVAL_LABELS = Path(__file__).resolve().parents[2] / "eval" / "labels.json"

# Таблицы, которые затрагивает пересборка, плюс служебный срез articles. Полные копии, а не
# только затронутые строки: восстановление проще и не зависит от того, как выбиралось окно.
BACKUP_TABLES = [
    ("events", "select * from events"),
    ("event_articles", "select * from event_articles"),
    ("claim_groups", "select * from claim_groups"),
    ("claim_group_members", "select * from claim_group_members"),
    ("renders", "select * from renders"),
    ("render_claims", "select * from render_claims"),
    ("stories", "select * from stories"),
    ("story_events", "select * from story_events"),
    ("entities", "select * from entities"),
    ("entity_aliases", "select * from entity_aliases"),
    ("article_entities", "select * from article_entities"),
    # entities_extracted_at сбрасывается у статей пересборки; без этого среза не узнать, у каких.
    ("articles_entities_extracted_at", "select id, entities_extracted_at from articles"),
]


def labeled_article_ids() -> list[str]:
    data = json.loads(EVAL_LABELS.read_text(encoding="utf-8"))
    return [a for cluster in data["clusters"].values() for a in cluster["articleIds"]]


def seeded_entity_names() -> list[str]:
    path = config.PACKAGE_DIR / "aliases_seed.yaml"
    return [e["canonical_name"] for e in yaml.safe_load(path.read_text(encoding="utf-8"))["entities"]]


def select_scope(conn, days: int, labeled_ids: list[str]) -> dict:
    """Что затронет пересборка. Ничего не меняет.

    events          — события, которые будут удалены (с окном и размеченными статьями, плюс слитые,
                      указывающие на них, по цепочке);
    articles        — все статьи этих событий (они останутся без события и пройдут кластеризацию);
    window_articles — из них моложе days суток;
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct ea.event_id
            from event_articles ea join articles a on a.id = ea.article_id
            where a.published_at > now() - make_interval(days => %s) or a.id = any(%s::uuid[])
            """,
            (days, labeled_ids),
        )
        direct = [row["event_id"] for row in cur.fetchall()]

        # Замыкание по canonical_id: слитое событие, чья цель удаляется, удаляется тоже.
        events = set(direct)
        frontier = list(direct)
        while frontier:
            cur.execute("select id from events where canonical_id = any(%s)", (frontier,))
            frontier = [row["id"] for row in cur.fetchall() if row["id"] not in events]
            events.update(frontier)

        cur.execute("select article_id from event_articles where event_id = any(%s)", (list(events),))
        articles = [row["article_id"] for row in cur.fetchall()]

        cur.execute(
            "select count(*) as n from articles where id = any(%s) and published_at > now() - make_interval(days => %s)",
            (articles, days),
        )
        window_articles = cur.fetchone()["n"]

        cur.execute("select count(*) as n from renders where event_id = any(%s)", (list(events),))
        renders = cur.fetchone()["n"]
        cur.execute("select count(*) as n from claim_groups where event_id = any(%s)", (list(events),))
        groups = cur.fetchone()["n"]

    return {
        "events": list(events),
        "direct_events": len(direct),
        "merged_pointers": len(events) - len(direct),
        "articles": articles,
        "window_articles": window_articles,
        "renders": renders,
        "claim_groups": groups,
    }


def orphan_entity_ids(conn, seeded: list[str]) -> list[str]:
    """Сущности без единой статьи (после сброса), кроме посевных из aliases_seed.yaml."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select e.id from entities e
            where not exists (select 1 from article_entities ae where ae.entity_id = e.id)
              and not (e.canonical_name = any(%s))
            """,
            (seeded,),
        )
        return [row["id"] for row in cur.fetchall()]


def backup(conn, out_dir: Path) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for name, query in BACKUP_TABLES:
        path = out_dir / f"{name}.csv"
        with conn.cursor() as cur, path.open("w", encoding="utf-8", newline="") as f:
            with cur.copy(f"copy ({query}) to stdout with csv header") as copy:
                for chunk in copy:
                    f.write(bytes(chunk).decode("utf-8"))
        with path.open(encoding="utf-8", newline="") as f:
            counts[name] = max(sum(1 for _ in csv.reader(f)) - 1, 0)
        logger.info("копия %s: %s строк -> %s", name, counts[name], path)
    return counts


def delete_scope(conn, scope: dict, seeded: list[str], commit: bool = True) -> dict:
    """Удаление одной транзакцией: либо всё, либо ничего. commit=False — вызывающий сам
    управляет транзакцией (тесты откатывают её)."""
    stats: dict = {}
    events, articles = scope["events"], scope["articles"]
    if commit:
        conn.autocommit = False
    try:
        with conn.cursor() as cur:
            # Сначала слитые указатели, потом их цели: внешний ключ canonical_id — no action.
            cur.execute("delete from events where id = any(%s) and status = 'merged'", (events,))
            stats["merged_deleted"] = cur.rowcount
            cur.execute("delete from events where id = any(%s)", (events,))
            stats["events_deleted"] = cur.rowcount

            cur.execute("delete from article_entities where article_id = any(%s)", (articles,))
            stats["article_entities_deleted"] = cur.rowcount
            cur.execute("update articles set entities_extracted_at = null where id = any(%s)", (articles,))
            stats["articles_reset"] = cur.rowcount

        orphans = orphan_entity_ids(conn, seeded)
        with conn.cursor() as cur:
            cur.execute("delete from entities where id = any(%s)", (orphans,))
            stats["orphan_entities_deleted"] = cur.rowcount
        if commit:
            conn.commit()
    except BaseException:
        if commit:
            conn.rollback()
        raise
    finally:
        if commit:
            conn.autocommit = True
    return stats


def delete_stories(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from story_events")
        links = cur.fetchone()["n"]
        cur.execute("delete from stories")
        return {"stories_deleted": cur.rowcount, "story_events_deleted": links}


def _print_scope(scope: dict, orphans: int | None = None) -> None:
    print(f"событий к удалению: {len(scope['events'])} (с окном/размеченными: {scope['direct_events']}, "
          f"слитых указателей на них: {scope['merged_pointers']})")
    print(f"статей без события после удаления: {len(scope['articles'])} (из них моложе окна: {scope['window_articles']})")
    print(f"сводок (renders) удалится: {scope['renders']}, групп фактов: {scope['claim_groups']}")
    if orphans is not None:
        print(f"осиротевших сущностей к удалению: {orphans}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="hotaru-rebuild", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("plan", "backup", "delete", "stories"))
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--labeled", action="store_true", help="включить события с размеченными статьями (eval/labels.json)")
    parser.add_argument("--out", type=Path, help="папка для backup")
    parser.add_argument("--yes", action="store_true", help="подтверждение удаления (delete, stories)")
    args = parser.parse_args()

    cfg = config.load()
    labeled = labeled_article_ids() if args.labeled else []
    with db.connect(cfg.database_url) as conn:
        if args.command == "stories":
            if not args.yes:
                raise SystemExit("удаление сюжетов требует --yes")
            print(delete_stories(conn))
            return
        scope = select_scope(conn, args.days, labeled)
        if args.command == "plan":
            _print_scope(scope)
            return
        if args.command == "backup":
            if not args.out:
                raise SystemExit("backup требует --out ПАПКА")
            print(json.dumps(backup(conn, args.out / datetime.now().strftime("%Y%m%d-%H%M%S")), ensure_ascii=False))
            return
        if not args.yes:
            raise SystemExit("delete требует --yes (и свежую копию: hotaru-rebuild backup)")
        print(json.dumps(delete_scope(conn, scope, seeded_entity_names()), ensure_ascii=False))


if __name__ == "__main__":
    main()
