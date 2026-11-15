import logging

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from psycopg import OperationalError

from . import claim_extraction, config, db, quota, runlog

logger = logging.getLogger("hotaru_worker.extract_claims")

BATCH_SIZE = 50
MAX_CONSECUTIVE_FAILURES = 5
# См. embeddings.py: без явного таймаута зависший запрос к Gemini не попадает ни
# в один except и висит часами с нулевой загрузкой CPU (случай 2026-11-03).
_HTTP_TIMEOUT_MS = 30_000


def _add_arguments(parser) -> None:
    parser.add_argument(
        "--scope",
        choices=("all", "multi", "rest"),
        default="all",
        help="multi — статьи свежих событий с 2+ изданиями; rest — остальные свежие; all — обе группы",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="не больше N статей за запуск (чтобы очередь rest не съедала квоту, нужную очереди multi)",
    )


def _is_permanent_error(exc: Exception) -> bool:
    """Постоянная ошибка запроса (400/404...), а не перегрузка: статья «отравлена». 429, 5xx
    и сетевые сбои — не вина статьи, они не должны выбивать её из очереди."""
    return isinstance(exc, genai_errors.APIError) and 400 <= exc.code < 500 and exc.code != 429


def run_once() -> dict:
    cfg = config.load()
    client = genai.Client(http_options=types.HttpOptions(timeout=_HTTP_TIMEOUT_MS))
    scope = getattr(runlog.args(), "scope", "all")
    max_items = getattr(runlog.args(), "max_items", None)

    stats = {"scope": scope, "processed": 0, "claims_found": 0, "skipped_llm_unavailable": 0}
    failed_this_run: list = []

    # Новое соединение на каждый батч — тот же урок, что в extract_entities.py и
    # embed_articles.py (соединение простаивает на время LLM-вызова, пулер его рвёт).
    consecutive_failures = 0
    while True:
        try:
            with db.connect(cfg.database_url) as conn:
                batch_size = BATCH_SIZE if max_items is None else min(BATCH_SIZE, max_items - stats["processed"])
                if batch_size <= 0:
                    break
                batch = db.articles_missing_claims(conn, batch_size, scope, exclude=failed_this_run)
                if not batch:
                    break
                made_progress = False
                quota_exhausted = False
                time_up = False
                for article in batch:
                    if runlog.out_of_time():
                        time_up = True
                        break
                    try:
                        claims = claim_extraction.extract_claims(client, article)
                    except (genai_errors.APIError, httpx.TransportError) as exc:
                        logger.warning("Gemini недоступна для статьи %s (%s), пропускаю", article["id"], exc)
                        stats["skipped_llm_unavailable"] += 1
                        failed_this_run.append(article["id"])
                        if _is_permanent_error(exc) and not quota.is_daily_quota(exc):
                            db.bump_claims_attempts(conn, article["id"])
                        if quota.is_daily_quota(exc):
                            # Дневная квота обеих моделей — остальные статьи
                            # получат то же самое, завершаем прогон (2026-11-09).
                            quota_exhausted = True
                            break
                        continue
                    db.insert_claims(conn, article["id"], claims)
                    db.mark_claims_extracted(conn, article["id"])
                    stats["processed"] += 1
                    stats["claims_found"] += len(claims)
                    made_progress = True
                if quota_exhausted or time_up:
                    break
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
    runlog.run("extract-claims", run_once, add_arguments=_add_arguments)


if __name__ == "__main__":
    main()
