import logging

from . import config, db, monitoring, runlog

logger = logging.getLogger("hotaru_worker.monitor")


def run_once() -> dict:
    """Раздел 6 CLAUDE.md: прогоняет все 4 проверки один раз, пишет каждый результат
    в monitoring_checks (исторический след, не только лог) и возвращает сводку алертов.
    Не вызывает внешние каналы уведомлений — их в проекте пока нет (см. tmp/plans)."""
    cfg = config.load()
    alerts: list[str] = []

    with db.connect(cfg.database_url) as conn:
        for row in monitoring.check_silent_sources(conn):
            db.save_monitoring_check(conn, "source_silent_24h", row["status"], row)
            if row["status"] == "alert":
                msg = f"источник {row['source_name']!r}: 0 статей за {row['window_hours']}ч"
                alerts.append(msg)
                logger.warning("АЛЕРТ source_silent_24h: %s", row)

        for row in monitoring.check_text_length_drop(conn):
            db.save_monitoring_check(conn, "text_length_drop", row["status"], row)
            if row["status"] == "alert":
                msg = (
                    f"источник {row['source_name']!r}: средняя длина текста упала с "
                    f"{row['baseline_avg_length']:.0f} до {row['recent_avg_length']:.0f}"
                )
                alerts.append(msg)
                logger.warning("АЛЕРТ text_length_drop: %s", row)

        rejection = monitoring.check_render_rejection_rate(conn)
        db.save_monitoring_check(conn, "render_rejection_rate", rejection["status"], rejection)
        if rejection["status"] == "alert":
            alerts.append(f"доля отклонённых генераций выше порога: {rejection}")
            logger.warning("АЛЕРТ render_rejection_rate: %s", rejection)

        gray_zone = monitoring.check_gray_zone_share(conn)
        db.save_monitoring_check(conn, "gray_zone_share", gray_zone["status"], gray_zone)
        if gray_zone["status"] == "alert":
            alerts.append(f"доля серой зоны {gray_zone['share']:.0%} выше порога")
            logger.warning("АЛЕРТ gray_zone_share: %s", gray_zone)

    if alerts:
        logger.warning("мониторинг завершён, алертов: %s — %s", len(alerts), alerts)
    else:
        logger.info("мониторинг завершён, алертов нет")
    return {"alerts": alerts}


def main() -> None:
    runlog.run("monitor", run_once)


if __name__ == "__main__":
    main()
