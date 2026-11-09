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
    # на одном батче навсегда (2026-11-05). Но если настроен Groq-фолбэк
    # (2026-11-08) — смоук-тест только Gemini больше не надёжен: Gemini может
    # быть мёртв, а Groq жив, и per-claim цепочка (claim_grouping.py) сама
    # разберётся. Пропускаем ранний выход, когда есть кем подстраховаться — но
    # смоук-тест всё равно даёт самый дешёвый сигнал "Gemini уже мёртв сейчас",
    # так что сразу помечаем quota.mark_gemini_dead() (2026-11-09, живой тест
    # показал: без этого первые ~N claims всё равно жгут retry-бюджет Gemini,
    # прежде чем per-claim код сам это обнаружит).
    gemini_ok = quota.has_quota(client, claim_grouping.MODEL)
    if not gemini_ok:
        quota.mark_gemini_dead()
        if not cfg.groq_api_key:
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
                    except (genai_errors.APIError, httpx.TransportError, httpx.HTTPStatusError, ValueError, KeyError) as exc:
                        # ValueError/KeyError — Groq без схемной привязки может
                        # вернуть JSON без ключа "relation" или с посторонним
                        # значением (synthesis.py поймал аналогичный краш на
                        # synthesize, 2026-11-09) — трактуем как обычный сбой,
                        # не квоту.
                        # Смоук-тест перед батчем (выше) ловит только "квота уже
                        # мёртва на старте" — если она умирает СЕРЕДИНЕ батча,
                        # каждый claim по-прежнему жжёт свой полный retry-бюджет
                        # (_classify_relation: до 2 моделей Gemini x MAX_RETRIES с
                        # backoff до 60с, плюс Groq, если настроен, до 3 кандидатных
                        # групп на claim — реально до нескольких минут на ОДИН claim),
                        # прежде чем попасть в этот except. На 50 claims это часы
                        # впустую (обнаружено 2026-11-06: прогон провисел >1ч).
                        # 429 (от Gemini ИЛИ, после добавления Groq 2026-11-08, от
                        # него тоже) — трактуем как "квота кончилась именно сейчас" и
                        # прекращаем весь прогон немедленно, не тратя бюджет retry
                        # на заведомо обречённые оставшиеся claims. Другие транзиентные
                        # ошибки (5xx, сеть) — по-прежнему пропускаем claim и идём дальше,
                        # это реальные отдельные сбои, не признак дневного лимита.
                        is_quota_wall = (
                            (isinstance(exc, genai_errors.APIError) and exc.code == 429)
                            or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429)
                        )
                        logger.warning("Gemini/Groq недоступны для claim %s (%s), пропускаю", claim["id"], exc)
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
