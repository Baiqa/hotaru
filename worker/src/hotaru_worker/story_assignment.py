import logging

from . import config, db, runlog

logger = logging.getLogger("hotaru_worker.story_assignment")

# Раздел 3.3 CLAUDE.md: сюжет — "цепочка событий: решение -> реакция ->
# последствие", недели-месяцы, низкий порог. В отличие от event_merge.py
# (который требует ещё majority-entity overlap, чтобы не склеить два события
# по общей NER-шумовой "сущности" — см. tmp/plans/etap1-kalibrovka-po-keisam.md),
# здесь majority-overlap был бы неверен по конструкции: сущности в сюжете
# естественно дрейфуют между первым и последним событием цепочки. Поэтому
# только мягкий косинус-порог + хотя бы одна общая сущность (уже обеспечена
# отбором кандидатов в find_similar_stories).
# Первая версия сравнивала событие с центроидом сюжета при пороге 0.55 и дала
# сюжеты на 376/295/95 событий: центроид дрейфует к «средней новости» (медиана
# попарного косинуса событий в корпусе 0.545). Теперь — максимум косинуса по
# последним RECENT_K участникам (CLAUDE.md §3.3, «дрейф»), порог 0.78 выбран по
# offline-эксперименту на 930 событиях: при 0.75-0.80 крупнейшие сюжеты 7-20
# событий. Размеченных данных на уровне сюжетов нет — порог неоткалиброван.
STORY_THRESHOLD = 0.78
STORY_WINDOW_DAYS = 60  # "недели-месяцы" по CLAUDE.md §3.3
CANDIDATE_LIMIT = 5
RECENT_K = 3
MIN_SHARING_MEMBERS = 2  # общая сущность хотя бы у 2 из последних участников
MAX_STORY_SIZE = 40  # страховка от цепного разрастания


def run_once(conn) -> dict:
    """Единый прогон на всё множество событий без сюжета — то же обоснование,
    что у event_merge.run_once: рабочее множество ограничено (не растущий
    поток статей), батч-реконнект не нужен."""
    events = db.events_missing_story(conn)
    stats = {"processed": 0, "new_stories": 0, "joined_existing": 0}

    for event in events:
        if runlog.out_of_time():
            break
        entity_ids = db.event_entity_ids(conn, event["id"])
        story_id = None
        if entity_ids:
            candidates = db.find_similar_stories(
                conn,
                event["centroid"],
                entity_ids,
                STORY_WINDOW_DAYS,
                event["started_at"],
                CANDIDATE_LIMIT,
                RECENT_K,
                MIN_SHARING_MEMBERS,
                MAX_STORY_SIZE,
            )
            if candidates and candidates[0]["cosine_sim"] >= STORY_THRESHOLD:
                story_id = candidates[0]["id"]

        if story_id:
            db.assign_event_to_story(conn, event["id"], story_id)
            stats["joined_existing"] += 1
        else:
            db.create_story(conn, event)
            stats["new_stories"] += 1
        stats["processed"] += 1
        if stats["processed"] % 50 == 0:
            logger.info("прогресс: %s/%s, %s", stats["processed"], len(events), stats)

    logger.info("привязка событий к сюжетам завершена: %s", stats)
    return stats


def _run() -> dict:
    cfg = config.load()
    with db.connect(cfg.database_url) as conn:
        return run_once(conn)


def main() -> None:
    runlog.run("assign-stories", _run)


if __name__ == "__main__":
    main()
