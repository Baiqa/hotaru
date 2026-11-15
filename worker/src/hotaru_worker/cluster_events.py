import logging

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from psycopg import OperationalError

from . import clustering, config, db, gray_zone, runlog

logger = logging.getLogger("hotaru_worker.cluster_events")

BATCH_SIZE = 50
MAX_CONSECUTIVE_FAILURES = 5
# См. embeddings.py: без явного таймаута зависший запрос к Gemini не попадает ни
# в один except и висит часами с нулевой загрузкой CPU (случай 2026-11-03).
_HTTP_TIMEOUT_MS = 30_000
# Пакетная классификация серой зоны (фаза 5.4 плана P0) ВЫКЛЮЧЕНА по умолчанию: проверка
# качества (eval/gray_batch_check.py, 201 пара поровну same/related/different) 2026-11-15
# дала согласие с кешем 78% при 0 замен different -> same, но почти весь прогон ответила
# слабая третья модель цепочки (дневная квота основной и Groq была исчерпана), поэтому
# результат не показателен. Включить (12), когда повтор на основной модели даст >= 90%.
DEFAULT_GRAY_BATCH_SIZE = 1


def _add_arguments(parser) -> None:
    parser.add_argument(
        "--gray-batch-size",
        type=int,
        default=DEFAULT_GRAY_BATCH_SIZE,
        help="пар серой зоны в одном запросе к LLM; 1 — без пакетов (по одной паре, как раньше)",
    )


def run_once() -> dict:
    cfg = config.load()
    client = genai.Client(http_options=types.HttpOptions(timeout=_HTTP_TIMEOUT_MS))
    gray_batch_size = getattr(runlog.args(), "gray_batch_size", DEFAULT_GRAY_BATCH_SIZE)

    stats = {"processed": 0, "new_events": 0, "joined": 0, "skipped_llm_unavailable": 0}

    # Новое соединение на каждый батч (тот же урок, что в extract_entities.py и
    # embed_articles.py), но порядок обработки строго последовательный
    # (db.articles_missing_event упорядочивает по published_at) — это важно для
    # корректности инкрементального алгоритма, не только для устойчивости к обрыву.
    consecutive_failures = 0
    while True:
        try:
            with db.connect(cfg.database_url) as conn:
                batch = db.articles_missing_event(conn, BATCH_SIZE)
                if not batch:
                    break
                made_progress = False
                quota_exhausted = False
                time_up = False
                prefetched: dict = {}
                if gray_batch_size > 1:
                    try:
                        prefetched = clustering.prefetch_gray_zone(conn, client, batch, gray_batch_size)
                    except gray_zone.DailyQuotaExceeded as exc:
                        logger.warning("дневная квота LLM исчерпана (%s), завершаю прогон", exc)
                        stats["skipped_llm_unavailable"] += 1
                        break
                    except (genai_errors.APIError, httpx.TransportError, httpx.HTTPStatusError) as exc:
                        # Пакет не удался — не страшно: ниже каждая пара пойдёт одиночным запросом.
                        logger.warning("пакетный запрос серой зоны не удался (%s), иду по одной паре", exc)
                for article in batch:
                    if runlog.out_of_time():
                        time_up = True
                        break
                    # Батч выбран один раз в начале итерации — пока обрабатывается
                    # более ранняя статья, самовосстановление в clustering.py
                    # (_resolve_join_event) могло уже создать событие для более
                    # поздней статьи из этого же батча, если та оказалась лучшим
                    # кандидатом. Пропускаем, иначе повторная вставка в
                    # event_articles упадёт на дубликате ключа (article_id — PK).
                    if db.event_id_for_article(conn, article["id"]) is not None:
                        made_progress = True
                        continue
                    try:
                        _, created = clustering.cluster_article(conn, client, article, prefetched.get(article["id"]))
                    except gray_zone.DailyQuotaExceeded as exc:
                        # Дневная квота кончилась у всех провайдеров: повторы
                        # бессмысленны (замер 2026-11-09: 70 минут впустую на
                        # 210 статей). Статьи остаются в articles_missing_event.
                        logger.warning("дневная квота LLM исчерпана (%s), завершаю прогон", exc)
                        stats["skipped_llm_unavailable"] += 1
                        quota_exhausted = True
                        break
                    except (genai_errors.APIError, httpx.TransportError, httpx.HTTPStatusError) as exc:
                        # Gemini недоступна даже после всех retry внутри gray_zone.classify()
                        # (реальный случай 2026-09-27/28 — устойчивая перегрузка free-tier
                        # моделей, плюс отдельно сетевые обрывы вроде httpx.ReadError).
                        # Пропускаем эту статью, не роняем весь прогон — она останется в
                        # articles_missing_event и попадёт в следующий запуск.
                        logger.warning("Gemini недоступна для статьи %s (%s), пропускаю", article["id"], exc)
                        stats["skipped_llm_unavailable"] += 1
                        continue
                    stats["processed"] += 1
                    stats["new_events" if created else "joined"] += 1
                    made_progress = True
                if quota_exhausted or time_up:
                    break
                if not made_progress:
                    # Весь батч пропущен (квота/перегрузка Gemini): пропущенные статьи
                    # остаются в articles_missing_event, и без этого выхода внешний
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

    stats.update(gray_zone.usage())
    logger.info("кластеризация событий завершена: %s", stats)
    return stats


def main() -> None:
    runlog.run("cluster-events", run_once, add_arguments=_add_arguments)


if __name__ == "__main__":
    main()
