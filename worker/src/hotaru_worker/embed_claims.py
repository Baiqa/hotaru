import logging

from google.genai import errors as genai_errors
from psycopg import OperationalError

from . import config, db, embeddings, quota

logger = logging.getLogger("hotaru_worker.embed_claims")

BATCH_SIZE = 50
MAX_CONSECUTIVE_FAILURES = 5


def run_once() -> dict:
    cfg = config.load()
    client = embeddings.build_client()

    stats = {"processed": 0}

    # Новое соединение на каждый батч — тот же урок, что и в embed_articles.py.
    consecutive_failures = 0
    while True:
        try:
            with db.connect(cfg.database_url) as conn:
                batch = db.claims_missing_embedding(conn, BATCH_SIZE)
                if not batch:
                    break
                quota_exhausted = False
                for claim in batch:
                    try:
                        vector = embeddings.embed_text(client, claim["text"])
                    except genai_errors.APIError as exc:
                        if not quota.is_daily_quota(exc):
                            raise
                        # Дневная квота эмбеддингов исчерпана — штатное завершение
                        # шага (см. embed_articles.py), остаток подберёт следующий запуск.
                        logger.warning("дневная квота эмбеддингов исчерпана, завершаю прогон")
                        quota_exhausted = True
                        break
                    db.update_claim_embedding(conn, claim["id"], vector)
                    stats["processed"] += 1
                if quota_exhausted:
                    break
            consecutive_failures = 0
        except OperationalError:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.exception("соединение с БД обрывается уже %s раз подряд, останавливаюсь", consecutive_failures)
                raise
            logger.warning("соединение с БД прервалось, переподключаюсь и продолжаю с необработанных claims")

    logger.info("вычисление эмбеддингов claims завершено: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_once()


if __name__ == "__main__":
    main()
