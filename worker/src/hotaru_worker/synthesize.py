import logging

import httpx
from google.genai import errors as genai_errors
from psycopg import OperationalError

from . import config, db, quota, synthesis

logger = logging.getLogger("hotaru_worker.synthesize")

BATCH_SIZE = 20
MAX_CONSECUTIVE_FAILURES = 5
FORMATS = ("summary_30s", "full_breakdown")


def run_once() -> dict:
    cfg = config.load()
    client = synthesis.build_client()

    # events_missing_render не помечает событие как обработанное при пропуске —
    # без этой проверки исчерпанная дневная квота зацикливает внешний while True
    # на одном батче навсегда (2026-11-05).
    if not quota.has_quota(client, synthesis.MODEL):
        return {"events_processed": 0, "renders_written": 0, "skipped": 0, "skipped_quota_exhausted": True}

    stats = {"events_processed": 0, "renders_written": 0, "skipped": 0}

    # Новое соединение на каждый батч — тот же урок, что в cluster_events.py и
    # group_claims.py.
    consecutive_failures = 0
    while True:
        try:
            with db.connect(cfg.database_url) as conn:
                batch = db.events_missing_render(conn, BATCH_SIZE)
                if not batch:
                    break
                for event in batch:
                    facts = db.event_facts_for_synthesis(conn, event["id"])
                    if not facts:
                        continue
                    event_ok = True
                    for format in FORMATS:
                        try:
                            result = synthesis.generate_render(client, event["id"], facts, format)
                        except (genai_errors.APIError, httpx.TransportError) as exc:
                            logger.warning("Gemini недоступна для события %s (%s), пропускаю", event["id"], exc)
                            event_ok = False
                            break
                        if result is None:
                            event_ok = False
                            continue
                        body, sentence_claims = result
                        db.save_render(conn, event["id"], format, "ru", body, synthesis.MODEL, sentence_claims)
                        stats["renders_written"] += 1
                    stats["events_processed" if event_ok else "skipped"] += 1
            consecutive_failures = 0
        except OperationalError:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.exception("соединение с БД обрывается уже %s раз подряд, останавливаюсь", consecutive_failures)
                raise
            logger.warning("соединение с БД прервалось, переподключаюсь и продолжаю с необработанных событий")

    logger.info("синтез текста событий завершён: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_once()


if __name__ == "__main__":
    main()
