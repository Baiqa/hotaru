import logging

from psycopg import OperationalError

from . import config, db, embeddings

logger = logging.getLogger("hotaru_worker.embed_articles")

BATCH_SIZE = 50
MAX_CONSECUTIVE_FAILURES = 5


def run_once() -> dict:
    cfg = config.load()
    client = embeddings.build_client()

    stats = {"processed": 0}

    # Новое соединение на каждый батч — тот же урок, что и в extract_entities.py:
    # пока считается эмбеддинг (сетевой вызов к Gemini), соединение простаивает,
    # и долгоживущее соединение рискует быть разорвано пулером Supabase.
    consecutive_failures = 0
    while True:
        try:
            with db.connect(cfg.database_url) as conn:
                batch = db.articles_missing_embedding(conn, BATCH_SIZE)
                if not batch:
                    break
                for article in batch:
                    text = embeddings.article_text(article)
                    vector = embeddings.embed_text(client, text)
                    db.update_embedding(conn, article["id"], vector)
                    stats["processed"] += 1
            consecutive_failures = 0
        except OperationalError:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.exception("соединение с БД обрывается уже %s раз подряд, останавливаюсь", consecutive_failures)
                raise
            logger.warning("соединение с БД прервалось, переподключаюсь и продолжаю с необработанных статей")

    logger.info("вычисление эмбеддингов завершено: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_once()


if __name__ == "__main__":
    main()
