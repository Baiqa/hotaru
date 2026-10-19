import logging

from psycopg import OperationalError

from . import config, db, entities

logger = logging.getLogger("hotaru_worker.extract_entities")

BATCH_SIZE = 50
MAX_CONSECUTIVE_FAILURES = 5


def run_once() -> dict:
    cfg = config.load()
    aliases = config.load_aliases(cfg.aliases_file)
    models = entities.load_models()

    stats = {"processed": 0, "entities_found": 0, "skipped_no_model": 0}

    with db.connect(cfg.database_url) as conn:
        db.seed_aliases(conn, aliases)

    # Новое соединение на каждый батч (а не одно на весь прогон): пока spaCy
    # разбирает статью (полный pipeline с parser'ом небыстрый), соединение
    # простаивает — на реальном прогоне это привело к разрыву пулером на
    # середине ("server closed the connection unexpectedly"). Повтор безопасен:
    # уже обработанные статьи не попадут в следующую выборку (entities_extracted_at).
    consecutive_failures = 0
    while True:
        try:
            with db.connect(cfg.database_url) as conn:
                batch = db.articles_missing_entities(conn, BATCH_SIZE)
                if not batch:
                    break
                for article in batch:
                    if article["lang"] not in models:
                        stats["skipped_no_model"] += 1
                    found = entities.process_article(conn, models, article)
                    stats["processed"] += 1
                    stats["entities_found"] += found
            consecutive_failures = 0
        except OperationalError:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.exception("соединение с БД обрывается уже %s раз подряд, останавливаюсь", consecutive_failures)
                raise
            logger.warning("соединение с БД прервалось, переподключаюсь и продолжаю с необработанных статей")

    logger.info("извлечение сущностей завершено: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_once()


if __name__ == "__main__":
    main()
