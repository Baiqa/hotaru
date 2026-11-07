import logging

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from psycopg import OperationalError

from . import claim_grouping, config, db, quota

logger = logging.getLogger("hotaru_worker.group_claims")

BATCH_SIZE = 50
MAX_CONSECUTIVE_FAILURES = 5
# См. embeddings.py: без явного таймаута зависший запрос к Gemini не попадает ни
# в один except и висит часами с нулевой загрузкой CPU (случай 2026-11-03).
_HTTP_TIMEOUT_MS = 30_000


def run_once() -> dict:
    cfg = config.load()
    client = genai.Client(http_options=types.HttpOptions(timeout=_HTTP_TIMEOUT_MS))

    # claims_missing_group не помечает claim как обработанный при пропуске —
    # без этой проверки исчерпанная дневная квота зацикливает внешний while True
    # на одном батче навсегда (2026-11-05).
    if not quota.has_quota(client, claim_grouping.MODEL):
        return {
            "processed": 0,
            "new_groups": 0,
            "confirmed": 0,
            "contradicted": 0,
            "skipped_llm_unavailable": 0,
            "skipped_quota_exhausted": True,
        }

    stats = {"processed": 0, "new_groups": 0, "confirmed": 0, "contradicted": 0, "skipped_llm_unavailable": 0}

    # Новое соединение на каждый батч — тот же урок, что в cluster_events.py.
    consecutive_failures = 0
    quota_died_mid_run = False
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
                        # Смоук-тест перед батчем (выше) ловит только "квота уже
                        # мёртва на старте" — если она умирает СЕРЕДИНЕ батча,
                        # каждый claim по-прежнему жжёт свой полный retry-бюджет
                        # (_classify_relation: до 2 моделей x MAX_RETRIES с backoff
                        # до 60с, до 3 кандидатных групп на claim — реально до
                        # нескольких минут на ОДИН claim), прежде чем попасть в этот
                        # except. На 50 claims это часы впустую (обнаружено 2026-11-06:
                        # прогон провисел >1ч, продвинувшись на considered claims).
                        # 429 — трактуем как "квота кончилась именно сейчас" и
                        # прекращаем весь прогон немедленно, не тратя бюджет retry
                        # на заведомо обречённые оставшиеся claims. Другие транзиентные
                        # ошибки (5xx, сеть) — по-прежнему пропускаем claim и идём дальше,
                        # это реальные отдельные сбои, не признак дневного лимита.
                        is_quota_wall = isinstance(exc, genai_errors.APIError) and exc.code == 429
                        logger.warning("Gemini недоступна для claim %s (%s), пропускаю", claim["id"], exc)
                        stats["skipped_llm_unavailable"] += 1
                        if is_quota_wall:
                            quota_died_mid_run = True
                            break
                        continue
                    stats["processed"] += 1
                    if outcome == "new":
                        stats["new_groups"] += 1
                    elif outcome == "confirms":
                        stats["confirmed"] += 1
                    elif outcome == "contradicts":
                        stats["contradicted"] += 1
            consecutive_failures = 0
            if quota_died_mid_run:
                stats["skipped_quota_exhausted"] = True
                break
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
