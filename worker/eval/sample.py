"""Выборка статей для ручной B-Cubed разметки (см. tmp/plans/etap1-bcubed-eval.md).

Берёт ВСЕ активные события с >= LARGE_EVENT_THRESHOLD статьями целиком (это
уже известные проблемные крупные кластеры — Минфин over-merge, Habr NER-шум),
плюс случайные события целиком, пока общее число статей не достигнет TARGET_TOTAL.

Запуск: uv run python eval/sample.py
Результат: eval/sample_export.json
"""

import json
import random
from datetime import datetime
from pathlib import Path

from hotaru_worker import config, db

LARGE_EVENT_THRESHOLD = 5
TARGET_TOTAL = 250
SEED = 42

OUTPUT_PATH = Path(__file__).resolve().parent / "sample_export.json"


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Не умею сериализовать {type(value)}")


def main() -> None:
    settings = config.load()
    rng = random.Random(SEED)

    with db.connect(settings.database_url) as conn:
        events = db.list_active_events(conn)

        large_events = [e for e in events if e["article_count"] >= LARGE_EVENT_THRESHOLD]
        small_events = [e for e in events if e["article_count"] < LARGE_EVENT_THRESHOLD]
        rng.shuffle(small_events)

        selected_events = list(large_events)
        total = sum(e["article_count"] for e in selected_events)
        for event in small_events:
            if total >= TARGET_TOTAL:
                break
            selected_events.append(event)
            total += event["article_count"]

        selected_event_ids = [e["id"] for e in selected_events]
        known_large_ids = {str(e["id"]) for e in large_events}

        articles = db.articles_for_events(conn, selected_event_ids)

    groups: dict[str, dict] = {}
    for event in selected_events:
        event_id = str(event["id"])
        groups[event_id] = {
            "event_id": event_id,
            "title": event["title"],
            "article_count": event["article_count"],
            "is_known_large": event_id in known_large_ids,
            "article_ids": [],
        }

    article_list = []
    for row in articles:
        event_id = str(row["event_id"])
        article_id = str(row["article_id"])
        groups[event_id]["article_ids"].append(article_id)
        article_list.append(
            {
                "article_id": article_id,
                "title": row["title"],
                "lead": row["lead"],
                "source_name": row["source_name"],
                "published_at": row["published_at"],
                "predicted_event_id": event_id,
            }
        )

    export = {
        "generated_at": datetime.now(),
        "seed": SEED,
        "large_event_threshold": LARGE_EVENT_THRESHOLD,
        "target_total": TARGET_TOTAL,
        "total_articles": len(article_list),
        "total_groups": len(groups),
        "known_large_event_count": len(known_large_ids),
        "groups": groups,
        "articles": article_list,
    }

    OUTPUT_PATH.write_text(
        json.dumps(export, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )

    print(f"Статей: {len(article_list)}, групп (событий): {len(groups)}")
    print(f"Из них известных крупных кластеров: {len(known_large_ids)}")
    print(f"Записано в {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
