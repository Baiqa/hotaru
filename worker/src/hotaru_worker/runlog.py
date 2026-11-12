"""Общая обвязка шагов конвейера: журнал запусков и бюджет времени.

Каждая точка входа (`hotaru-collect`, `hotaru-cluster-events`, ...) вызывает
`runlog.run("имя-шага", run_once)`. Это даёт три вещи.

1. Журнал. В таблицу `pipeline_runs` пишется строка при старте шага и закрывается
   при завершении (статус, время, статистика). Журнал — вспомогательный: если БД
   недоступна, шаг всё равно выполняется, а запись пропускается с предупреждением.

2. Бюджет времени, ключ `--max-minutes N`. Разбор отменённых запусков GitHub
   Actions (2026-11-12) показал: 19 из 20 отмен — это таймаут job на одном медленном
   шаге (Cluster events, Extract claims, Embed articles), после чего остальные шаги
   не выполнялись вовсе. Жёсткий таймаут убивает процесс; мягкий бюджет даёт шагу
   самому остановиться между элементами: раннеры спрашивают `out_of_time()` в начале
   обработки очередного элемента и выходят. Необработанное подберёт следующий запуск
   (все шаги идемпотентны).

3. Редкие шаги, ключ `--if-due-hours N`. Шаг выполняется, только если с его
   последнего успешного запуска прошло больше N часов. Так суточные задачи
   (разделение событий, ряды World Bank) живут в общем конвейере и не зависят от
   отдельного cron.
"""

import argparse
import json
import logging
import signal
import sys
import time
from collections.abc import Callable

from psycopg.types.json import Jsonb

from . import config, db

logger = logging.getLogger("hotaru_worker.runlog")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_MAX_ERROR_CHARS = 500

# Момент (time.monotonic), после которого шагу пора завершаться. None — без бюджета
# (локальный запуск без ключа --max-minutes ведёт себя как раньше).
_deadline: float | None = None
_budget_hit = False


def out_of_time() -> bool:
    """Исчерпан ли бюджет времени шага. Раннер вызывает это в начале обработки
    очередного элемента и, получив True, обязан выйти из цикла — поэтому сам факт
    ответа True запоминается и попадает в статистику запуска."""
    global _budget_hit
    if _deadline is not None and time.monotonic() >= _deadline:
        _budget_hit = True
        return True
    return False


def _dumps(obj) -> str:
    # default=str: в статистике встречаются UUID и datetime.
    return json.dumps(obj, default=str, ensure_ascii=False)


def _start(database_url: str, step: str) -> str | None:
    try:
        with db.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute("insert into pipeline_runs (step) values (%s) returning id", (step,))
            return cur.fetchone()["id"]
    except Exception as exc:
        logger.warning("журнал запусков недоступен, шаг %s выполняется без записи (%s)", step, exc)
        return None


def _finish(database_url: str, run_id: str | None, status: str, stats: dict) -> None:
    if run_id is None:
        return
    try:
        with db.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute(
                "update pipeline_runs set status = %s, finished_at = now(), stats = %s where id = %s",
                (status, Jsonb(stats, dumps=_dumps), run_id),
            )
    except Exception as exc:
        logger.warning("не удалось закрыть запись журнала запусков %s (%s)", run_id, exc)


def is_due(database_url: str, step: str, hours: float) -> bool:
    """Прошло ли больше `hours` часов с последнего УСПЕШНОГО запуска шага. Упавший
    запуск не считается: шаг повторится в следующем прогоне конвейера. Если журнал
    недоступен — считаем, что пора (лучше лишний запуск, чем пропущенный)."""
    try:
        with db.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute(
                """
                select 1 from pipeline_runs
                where step = %s and status = 'ok'
                  and started_at > now() - (%s * interval '1 hour')
                limit 1
                """,
                (step, hours),
            )
            return cur.fetchone() is None
    except Exception as exc:
        logger.warning("журнал запусков недоступен, считаю шаг %s назревшим (%s)", step, exc)
        return True


def _raise_on_sigterm(signum, frame) -> None:
    # GitHub Actions при жёстком таймауте шага шлёт SIGTERM. Превращаем его в
    # исключение, чтобы успеть закрыть запись журнала статусом failed.
    sys.exit(128 + signum)


def run(step: str, fn: Callable[[], dict | None], argv: list[str] | None = None) -> None:
    """Точка входа шага: разбирает ключи, ведёт журнал, выставляет бюджет времени.
    Исключение из `fn` записывается в журнал и пробрасывается дальше — код
    завершения процесса остаётся ненулевым, как и до появления журнала."""
    global _deadline, _budget_hit

    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)

    parser = argparse.ArgumentParser(prog=f"hotaru-{step}")
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        help="бюджет времени: по его истечении шаг завершает текущий элемент и выходит",
    )
    parser.add_argument(
        "--if-due-hours",
        type=float,
        default=None,
        help="выполнять, только если с последнего успешного запуска прошло больше N часов",
    )
    args = parser.parse_args(argv)

    cfg = config.load()

    if args.if_due_hours is not None and not is_due(cfg.database_url, step, args.if_due_hours):
        logger.info("шаг %s пропущен: успешный запуск был менее %g ч назад", step, args.if_due_hours)
        return

    signal.signal(signal.SIGTERM, _raise_on_sigterm)
    run_id = _start(cfg.database_url, step)

    # Отсчёт бюджета — от начала самой работы, а не от записи в журнал
    # (подключение к БД через пулер занимает 1–2 с).
    started = time.monotonic()
    _budget_hit = False
    _deadline = started + args.max_minutes * 60 if args.max_minutes is not None else None
    try:
        stats = fn()
    except BaseException as exc:  # включая SystemExit от SIGTERM и KeyboardInterrupt
        error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
        _finish(
            cfg.database_url,
            run_id,
            "failed",
            {"error": error, "seconds": round(time.monotonic() - started, 1)},
        )
        raise

    result = dict(stats or {})
    result["seconds"] = round(time.monotonic() - started, 1)
    if _budget_hit:
        result["stopped_by_time_budget"] = True
        logger.warning("шаг %s остановлен по бюджету времени (%g мин), остаток — в следующем запуске", step, args.max_minutes)
    _finish(cfg.database_url, run_id, "ok", result)
