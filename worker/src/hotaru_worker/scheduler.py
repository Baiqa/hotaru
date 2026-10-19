import logging
import time

from . import collect

# Раздел 5 CLAUDE.md, Этап 0: "Крон раз в 30 минут".
# Это простой цикл для локальной разработки — реальный хостинг/шедулер воркера
# ещё не выбран (раздел 8, открытые вопросы).
RUN_INTERVAL_SECONDS = 30 * 60


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("hotaru_worker.scheduler")
    logger.info("старт планировщика, интервал=%sс", RUN_INTERVAL_SECONDS)
    while True:
        try:
            collect.run_once()
        except Exception:
            logger.exception("прогон сбора упал, повтор через интервал")
        time.sleep(RUN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
