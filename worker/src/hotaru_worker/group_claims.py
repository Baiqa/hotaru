import logging

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from psycopg import OperationalError

from . import claim_grouping, config, db

logger = logging.getLogger("hotaru_worker.group_claims")

BATCH_SIZE = 50
MAX_CONSECUTIVE_FAILURES = 5
# См. embeddings.py: без явного таймаута зависший запрос к Gemini не попадает ни
# в один except и висит часами с нулевой загрузкой CPU (случай 2026-11-03).
_HTTP_TIMEOUT_MS = 30_000


def run_once() -> dict:
    cfg = config.load()
    client = genai.Client(http_options=types.HttpOptions(timeout=_HTTP_TIMEOUT_MS))

    stats = {"processed": 0, "new_groups": 0, "confirmed": 0, "contradicted": 0, "skipped_llm_unavailable": 0}

    # Новое соединение на каждый батч — тот же урок, что в cluster_events.py.
    consecutive_failures = 0
    while True:
        try:
            with db.connect(cfg.database_url) as conn:
                batch = db.claims_missing_group(conn, BATCH_SIZE)
                if not batch:
                    break
                for claim in batch:
                    try:
                        outcome = claim_grouping.group_claim(conn, client, claim, claim["event_id"])
                    except (genai_errors.APIError, httpx.TransportError) as exc:
                        logger.warning("Gemini недоступна для claim %s (%s), пропускаю", claim["id"], exc)
                        stats["skipped_llm_unavailable"] += 1
                        continue
                    stats["processed"] += 1
                    if outcome == "new":
                        stats["new_groups"] += 1
                    elif outcome == "confirms":
                        stats["confirmed"] += 1
                    elif outcome == "contradicts":
                        stats["contradicted"] += 1
            consecutive_failures = 0
        except OperationalError:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.exception("соединение с БД обрывается уже %s раз подряд, останавливаюсь", consecutive_failures)
                raise
            logger.warning("соединение с БД прервалось, переподключаюсь и продолжаю с необработанных claims")

    logger.info("схлопывание фактов завершено: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_once()


if __name__ == "__main__":
    main()
