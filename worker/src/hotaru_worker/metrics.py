import logging

import httpx

from . import config, db

logger = logging.getLogger("hotaru_worker.metrics")

WORLD_BANK_BASE = "https://api.worldbank.org/v2"

# Этап 3 MVP (раздел 3.5/8 CLAUDE.md) — единственный индикатор + два хардкод
# пары страна+индикатор. Расширять по мере роста покрытия claim_numbers.label/unit
# реальными экономическими числами (проверка 2026-11-05: подавляющее большинство
# чисел в корпусе — не экономические, инфляция % — самый частый реальный кандидат).
SERIES = [
    {
        "key": "FP.CPI.TOTL.ZG:KAZ",
        "indicator": "FP.CPI.TOTL.ZG",
        "country": "KAZ",
        "label": "Инфляция, % в год — Казахстан",
        "unit": "%",
    },
    {
        "key": "FP.CPI.TOTL.ZG:RUS",
        "indicator": "FP.CPI.TOTL.ZG",
        "country": "RUS",
        "label": "Инфляция, % в год — Россия",
        "unit": "%",
    },
]


def fetch_series(client: httpx.Client, indicator: str, country: str) -> list[dict]:
    url = f"{WORLD_BANK_BASE}/country/{country}/indicator/{indicator}"
    resp = client.get(url, params={"format": "json", "per_page": "100"})
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
        logger.warning("World Bank вернул пусто для %s/%s", indicator, country)
        return []
    return [{"period": row["date"], "value": row["value"]} for row in payload[1] if row["value"] is not None]


def run_once() -> dict:
    cfg = config.load()
    stats = {"series_updated": 0, "points_upserted": 0}

    with httpx.Client(timeout=cfg.http_timeout_seconds, headers={"User-Agent": cfg.user_agent}) as http_client:
        with db.connect(cfg.database_url) as conn:
            for series in SERIES:
                points = fetch_series(http_client, series["indicator"], series["country"])
                if not points:
                    continue
                series_id = db.upsert_metric_series(
                    conn,
                    series["key"],
                    series["label"],
                    series["unit"],
                    "World Bank",
                    f"https://data.worldbank.org/indicator/{series['indicator']}?locations={series['country']}",
                )
                db.upsert_metric_points(conn, series_id, points)
                conn.commit()
                stats["series_updated"] += 1
                stats["points_upserted"] += len(points)

    logger.info("загрузка метрик завершена: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_once()


if __name__ == "__main__":
    main()
