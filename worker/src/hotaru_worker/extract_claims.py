import logging

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from psycopg import OperationalError

from . import claim_extraction, config, db

logger = logging.getLogger("hotaru_worker.extract_claims")

BATCH_SIZE = 50
MAX_CONSECUTIVE_FAILURES = 5
# См. embeddings.py: без явного таймаута зависший запрос к Gemini не попадает ни
# в один except и висит часами с нулевой загрузкой CPU (случай 2026-11-03).
_HTTP_TIMEOUT_MS = 30_000


def run_once() -> dict:
    cfg = config.load()
    client = genai.Client(http_options=types.HttpOptions(timeout=_HTTP_TIMEOUT_MS))

    stats = {"processed": 0, "claims_found": 0, "skipped_llm_unavailable": 0}

    # Новое соединение на каждый батч — тот же урок, что в extract_entities.py и
    # embed_articles.py (соединение простаивает на время LLM-вызова, пулер его рвёт).
    consecutive_failures = 0
    while True:
        try:
            with db.connect(cfg.database_url) as conn:
                batch = db.articles_missing_claims(conn, BATCH_SIZE)
                if not batch:
                    break
                made_progress = False
                for article in batch:
                    try:
                        claims = claim_extraction.extract_claims(client, article)
                    except (genai_errors.APIError, httpx.TransportError) as exc:
                        logger.warning("Gemini недоступна для статьи %s (%s), пропускаю", article["id"], exc)
                        stats["skipped_llm_unavailable"] += 1
                        continue
                    db.insert_claims(conn, article["id"], claims)
                    db.mark_claims_extracted(conn, article["id"])
                    stats["processed"] += 1
                    stats["claims_found"] += len(claims)
                    made_progress = True
                if not made_progress:
                    # Весь батч пропущен (квота/перегрузка Gemini): пропущенные статьи
                    # остаются в articles_missing_claims, и без этого выхода внешний
                    # цикл вечно перечитывал бы тот же батч (см. quota.py, тот же баг
                    # был в group_claims/synthesize). Статьи подберёт следующий запуск.
                    logger.warning("ни одна статья батча не обработана, завершаю прогон")
                    break
            consecutive_failures = 0
        except OperationalError:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.exception("соединение с БД обрывается уже %s раз подряд, останавливаюсь", consecutive_failures)
                raise
            logger.warning("соединение с БД прервалось, переподключаюсь и продолжаю с необработанных статей")

    logger.info("извлечение фактов завершено: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_once()


if __name__ == "__main__":
    main()
