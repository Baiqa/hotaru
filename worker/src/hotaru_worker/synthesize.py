import logging

import httpx
from google.genai import errors as genai_errors
from psycopg import OperationalError

from . import config, db, quota, runlog, synthesis

logger = logging.getLogger("hotaru_worker.synthesize")

BATCH_SIZE = 20
MAX_CONSECUTIVE_FAILURES = 5
# Только краткая сводка: у full_breakdown на сайте нет экрана (web/src/app/events/[id]
# читает только summary_30s), а каждый формат — отдельный вызов из дневной квоты
# (фаза 5 плана P0). Включается обратно добавлением в кортеж, когда появится экран.
FORMATS = ("summary_30s",)
# Сколько событий с ОДНИМ изданием синтезировать за запуск. Очередь уже ставит события с
# 2+ изданиями первыми, но без потолка одиночные расходовали бы остаток дневной квоты за
# первые же запуски суток, и событиям с 2+ изданиями, появившимся позже, её не хватало.
# Все события получат текст после перехода на платный тариф — тогда потолок убирается.
MAX_SINGLE_EVENTS_PER_RUN = 10


def run_once() -> dict:
    cfg = config.load()
    client = synthesis.build_client()

    # events_missing_render не помечает событие как обработанное при пропуске —
    # без этой проверки исчерпанная дневная квота зацикливает внешний while True
    # на одном батче навсегда (2026-11-05). Но если настроен Groq-фолбэк
    # (2026-11-08) — смоук-тест только Gemini больше не надёжен: Gemini может
    # быть мёртв, а Groq жив, и generate_render сам разберётся через synthesis.py.
    # Смоук-тест всё равно даёт самый дешёвый сигнал "Gemini уже мёртв сейчас" —
    # сразу помечаем (2026-11-09, живой тест показал: без этого первые события
    # всё равно жгут retry-бюджет Gemini впустую).
    gemini_ok = quota.has_quota(client, synthesis.MODEL)
    if not gemini_ok:
        quota.mark_gemini_dead()
        if not cfg.groq_api_key:
            return {"events_processed": 0, "renders_written": 0, "skipped": 0, "skipped_quota_exhausted": True}

    stats = {"events_processed": 0, "renders_written": 0, "skipped": 0, "failed_generation": 0, "single_events": 0}

    # Новое соединение на каждый батч — тот же урок, что в cluster_events.py и
    # group_claims.py.
    consecutive_failures = 0
    quota_died_mid_run = False
    time_up = False
    singles_cap_reached = False
    failed_this_run: list = []
    while True:
        try:
            with db.connect(cfg.database_url) as conn:
                batch = db.events_missing_render(conn, BATCH_SIZE, exclude=failed_this_run)
                if not batch:
                    break
                made_progress = False
                for event in batch:
                    if runlog.out_of_time():
                        time_up = True
                        break
                    is_single = event["source_count"] < 2
                    if is_single and stats["single_events"] >= MAX_SINGLE_EVENTS_PER_RUN:
                        # Очередь отсортирована: события с 2+ изданиями идут первыми, значит
                        # дальше только одиночные — работа этого запуска закончена.
                        singles_cap_reached = True
                        break
                    facts = db.event_facts_for_synthesis(conn, event["id"])
                    if not facts:
                        failed_this_run.append(event["id"])
                        continue
                    event_ok = True
                    counted_failure = False
                    for format in FORMATS:
                        try:
                            result = synthesis.generate_render(client, event["id"], facts, format)
                        except (genai_errors.APIError, httpx.TransportError, httpx.HTTPStatusError, ValueError, KeyError) as exc:
                            # ValueError/KeyError — Groq без схемной привязки может
                            # вернуть JSON, не проходящий даже терпимый парсинг
                            # synthesis._parse_synthesis_json (pydantic ValidationError
                            # — подкласс ValueError — крашнула весь процесс, 2026-11-09).
                            # Трактуем как обычный сбой этого события, не квоту.
                            # Тот же урок, что в group_claims.py (2026-11-06):
                            # смоук-тест перед батчем не ловит квоту, умирающую
                            # СЕРЕДИНЕ батча — generate_render сам внутри уже
                            # жжёт MAX_GENERATION_RETRIES x (MODEL+FALLBACK+Groq,
                            # если настроен, x MAX_RETRIES с backoff) на КАЖДЫЙ
                            # формат. 429 (от Gemini ИЛИ, после добавления Groq
                            # 2026-11-08, от него тоже) — считаем квоту мёртвой и
                            # прекращаем весь прогон, не тратя этот бюджет на
                            # остальные события/форматы.
                            is_quota_wall = (
                                (isinstance(exc, genai_errors.APIError) and exc.code == 429)
                                or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429)
                            )
                            logger.warning("Gemini/Groq недоступны для события %s (%s), пропускаю", event["id"], exc)
                            event_ok = False
                            if is_quota_wall:
                                quota_died_mid_run = True
                            elif isinstance(exc, (ValueError, KeyError)):
                                # Модель вернула непригодный ответ — это провал генерации
                                # этого события. Сеть и 5xx — не его вина, не считаем.
                                counted_failure = True
                            break
                        if result is None:
                            # Пост-проверка отклонила текст даже после повторов.
                            event_ok = False
                            counted_failure = True
                            continue
                        body, sentence_claims, model_used = result
                        db.save_render(conn, event["id"], format, "ru", body, model_used, sentence_claims)
                        stats["renders_written"] += 1
                    if event_ok:
                        db.record_render_result(conn, event["id"], ok=True)
                        stats["events_processed"] += 1
                        made_progress = True
                    else:
                        stats["skipped"] += 1
                        failed_this_run.append(event["id"])
                        if counted_failure:
                            db.record_render_result(conn, event["id"], ok=False)
                            stats["failed_generation"] += 1
                    if is_single:
                        stats["single_events"] += 1
                    if quota_died_mid_run:
                        break
                if singles_cap_reached or time_up or quota_died_mid_run:
                    pass
                elif not made_progress:
                    # Ни одной новой сводки за проход батча (сбой/квота): без этого выхода
                    # внешний цикл перечитывал бы очередь снова и снова.
                    logger.warning("ни одна сводка батча не создана, завершаю прогон")
                    break
            consecutive_failures = 0
            if quota_died_mid_run:
                stats["skipped_quota_exhausted"] = True
                break
            if time_up or singles_cap_reached:
                break
        except OperationalError:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.exception("соединение с БД обрывается уже %s раз подряд, останавливаюсь", consecutive_failures)
                raise
            logger.warning("соединение с БД прервалось, переподключаюсь и продолжаю с необработанных событий")

    logger.info("синтез текста событий завершён: %s", stats)
    return stats


def main() -> None:
    runlog.run("synthesize", run_once)


if __name__ == "__main__":
    main()
