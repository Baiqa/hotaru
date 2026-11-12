import logging

from . import config, db, runlog, scoring

logger = logging.getLogger("hotaru_worker.event_merge")

# Раздел 3.3 CLAUDE.md: "Фоновая задача раз в час ищет пары близких событий и
# объединяет". Порог строже, чем порог присоединения статьи к событию (0.75) —
# здесь сравниваются центроиды (агрегаты многих статей), а не одна статья против
# одной, поэтому требуется более высокая уверенность, чтобы не склеить по шуму
# усреднения два реально разных события.
#
# Косинуса одного всё равно недостаточно: на реальных данных (B-Cubed разметка,
# см. tmp/plans/etap1-kalibrovka-po-keisam.md) диапазон косинуса у корректных слияний
# (0.69-0.94) и у ложного слияния 26 статей Habr в одно событие (0.70-0.88) пересекается
# — поднять порог нельзя, не сломав рабочие случаи. Разделяет их не косинус, а то, что у
# корректных слияний есть сущность, общая для большинства статей обоих событий (Трамп,
# CNN, Лавров...), а у Habr — ни одной. Поэтому merge требует ОБА сигнала.
MERGE_THRESHOLD = 0.85
WINDOW_DAYS = scoring.DEFAULT_WINDOW_DAYS
CANDIDATE_LIMIT = 5


def _pick_canonical(event_a: dict, event_b: dict) -> tuple[dict, dict]:
    """Больше статей — канонический; при равенстве — тот, что начался раньше."""
    if event_a["article_count"] != event_b["article_count"]:
        canonical, absorbed = (
            (event_a, event_b) if event_a["article_count"] > event_b["article_count"] else (event_b, event_a)
        )
    else:
        canonical, absorbed = (event_a, event_b) if event_a["started_at"] <= event_b["started_at"] else (event_b, event_a)
    return canonical, absorbed


def run_once(conn) -> dict:
    events = db.list_active_events(conn)
    stats = {"checked": len(events), "merged_pairs": 0}
    merged_ids: set[str] = set()

    for event in events:
        if runlog.out_of_time():
            break
        if event["id"] in merged_ids:
            continue
        entity_ids = db.event_entity_ids(conn, event["id"])
        if not entity_ids:
            continue
        candidates = db.find_similar_events(
            conn, event["id"], event["centroid"], entity_ids, WINDOW_DAYS, event["started_at"], CANDIDATE_LIMIT
        )
        event_majority_entities = None
        for candidate in candidates:
            if candidate["id"] in merged_ids or candidate["cosine_sim"] < MERGE_THRESHOLD:
                continue
            if event_majority_entities is None:
                event_majority_entities = db.event_majority_entities(conn, event["id"])
            candidate_majority_entities = db.event_majority_entities(conn, candidate["id"])
            if not (event_majority_entities & candidate_majority_entities):
                continue
            event_full = {**event}
            candidate_full = {"id": candidate["id"], "article_count": candidate["article_count"], "started_at": candidate["started_at"]}
            canonical, absorbed = _pick_canonical(event_full, candidate_full)
            db.merge_events(conn, canonical["id"], absorbed["id"])
            db.recompute_event_centroid(conn, canonical["id"])
            merged_ids.add(absorbed["id"])
            stats["merged_pairs"] += 1
            logger.info("слияние: %s -> %s (cosine=%.3f)", absorbed["id"], canonical["id"], candidate["cosine_sim"])
            if absorbed["id"] == event["id"]:
                break  # текущее событие поглощено, дальше не сравниваем

    logger.info("слияние событий завершено: %s", stats)
    return stats


def _run() -> dict:
    cfg = config.load()
    with db.connect(cfg.database_url) as conn:
        return run_once(conn)


def main() -> None:
    runlog.run("merge-events", _run)


if __name__ == "__main__":
    main()
