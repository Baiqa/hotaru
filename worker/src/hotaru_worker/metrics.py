import logging
import time

import httpx

from . import config, db, runlog

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


# API World Bank отвечает медленно и нестабильно: суточный запуск на GitHub падал
# на httpx.ReadTimeout при общем таймауте 15 с (2026-11-12, и ещё 2 из 3 запусков
# до того). Отдельный длинный таймаут и несколько попыток с паузой.
_TIMEOUT_SECONDS = 60.0
_ATTEMPTS = 3
_RETRY_PAUSE_SECONDS = 10


def _get_with_retry(client: httpx.Client, url: str, params: dict) -> httpx.Response:
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            resp = client.get(url, params=params)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp
            error: Exception = httpx.HTTPStatusError(
                f"World Bank вернул {resp.status_code}", request=resp.request, response=resp
            )
        except httpx.TransportError as exc:
            error = exc
        if attempt == _ATTEMPTS:
            raise error
        logger.warning("World Bank: %s, повтор через %sс (попытка %s/%s)", error, _RETRY_PAUSE_SECONDS, attempt, _ATTEMPTS)
        time.sleep(_RETRY_PAUSE_SECONDS)
    raise AssertionError("unreachable")


def fetch_series(client: httpx.Client, indicator: str, country: str) -> list[dict]:
    url = f"{WORLD_BANK_BASE}/country/{country}/indicator/{indicator}"
    resp = _get_with_retry(client, url, {"format": "json", "per_page": "100"})
    payload = resp.json()
    if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
        logger.warning("World Bank вернул пусто для %s/%s", indicator, country)
        return []
    return [{"period": row["date"], "value": row["value"]} for row in payload[1] if row["value"] is not None]


def run_once() -> dict:
    cfg = config.load()
    stats = {"series_updated": 0, "points_upserted": 0, "series_failed": 0}

    with httpx.Client(timeout=_TIMEOUT_SECONDS, headers={"User-Agent": cfg.user_agent}) as http_client:
        with db.connect(cfg.database_url) as conn:
            for series in SERIES:
                try:
                    points = fetch_series(http_client, series["indicator"], series["country"])
                except httpx.HTTPError as exc:
                    # Годовые ряды: один пропущенный день не критичен, а красный job из-за
                    # чужого API — шум. Сбой остаётся в статистике запуска (series_failed).
                    logger.warning("ряд %s не загружен после %s попыток: %s", series["key"], _ATTEMPTS, exc)
                    stats["series_failed"] += 1
                    continue
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
                stats["series_updated"] += 1
                stats["points_upserted"] += len(points)

    logger.info("загрузка метрик завершена: %s", stats)
    return stats


def main() -> None:
    runlog.run("fetch-metrics", run_once)


if __name__ == "__main__":
    main()
