"""Считает B-Cubed precision/recall: ручная разметка (eval/labels.json) против
текущей кластеризации событий в БД — см. tmp/plans/etap1-bcubed-eval.md.

Predicted-кластер каждой статьи читается live из БД (не из снимка sample.py),
т.к. merge/split фоновые задачи могли отработать между разметкой и подсчётом.

Запуск: uv run python eval/bcubed.py
"""

import json
from pathlib import Path

from hotaru_worker import config, db

EVAL_DIR = Path(__file__).resolve().parent
LABELS_PATH = EVAL_DIR / "labels.json"
SAMPLE_PATH = EVAL_DIR / "sample_export.json"
GOAL_THRESHOLD = 0.8


def load_ground_truth() -> dict[str, str]:
    """article_id -> ground_truth_cluster_key."""
    data = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    truth: dict[str, str] = {}
    for cluster_key, cluster in data["clusters"].items():
        for article_id in cluster["articleIds"]:
            truth[article_id] = cluster_key
    return truth


def bcubed(truth: dict[str, str], predicted: dict[str, str]) -> tuple[float, float]:
    """Стандартная формула B-Cubed, восстановленная относительно множества
    статей из `truth` (predicted может содержать более крупные кластеры —
    они обрезаются до этого множества, как того требует метод)."""
    article_ids = list(truth.keys())

    def members(mapping: dict[str, str], cluster_id) -> set[str]:
        return {a for a in article_ids if mapping.get(a) == cluster_id}

    precisions = []
    recalls = []
    for a in article_ids:
        c_a = members(predicted, predicted.get(a))
        l_a = members(truth, truth.get(a))
        overlap = len(c_a & l_a)
        precisions.append(overlap / len(c_a) if c_a else 0.0)
        recalls.append(overlap / len(l_a) if l_a else 0.0)

    precision = sum(precisions) / len(precisions)
    recall = sum(recalls) / len(recalls)
    return precision, recall


def main() -> None:
    if not LABELS_PATH.exists():
        print(f"Нет {LABELS_PATH} — сначала разметьте выборку в eval/labeling_tool/.")
        return

    truth = load_ground_truth()
    print(f"Размечено статей: {len(truth)}")

    settings = config.load()
    with db.connect(settings.database_url) as conn:
        predicted = db.event_ids_for_articles(conn, list(truth.keys()))

    missing = [a for a in truth if a not in predicted]
    if missing:
        print(f"Внимание: {len(missing)} размеченных статей не нашлись в event_articles "
              f"(могли быть удалены/пересозданы) — исключены из расчёта.")
        for a in missing:
            del truth[a]

    precision, recall = bcubed(truth, predicted)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    print()
    print(f"B-Cubed precision: {precision:.3f}")
    print(f"B-Cubed recall:    {recall:.3f}")
    print(f"B-Cubed F1:        {f1:.3f}")
    print(f"Порог готовности (CLAUDE.md §5): {GOAL_THRESHOLD}")
    status = "ДОСТИГНУТ" if precision >= GOAL_THRESHOLD and recall >= GOAL_THRESHOLD else "НЕ достигнут"
    print(f"Статус: {status}")

    if SAMPLE_PATH.exists():
        sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        known_large_articles = {
            aid
            for a in sample["articles"]
            for aid in [a["article_id"]]
            if sample["groups"].get(a["predicted_event_id"], {}).get("is_known_large")
        }
        known_large_articles &= set(truth.keys())
        if known_large_articles:
            sub_truth = {a: truth[a] for a in known_large_articles}
            p2, r2 = bcubed(sub_truth, predicted)
            print()
            print(f"Из них в заранее известных проблемных кластерах ({len(known_large_articles)} статей):")
            print(f"  precision: {p2:.3f}, recall: {r2:.3f}")

    report = {
        "labeled_articles": len(truth),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "goal_threshold": GOAL_THRESHOLD,
        "goal_met": precision >= GOAL_THRESHOLD and recall >= GOAL_THRESHOLD,
    }
    report_path = EVAL_DIR / "bcubed_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nОтчёт записан в {report_path}")


if __name__ == "__main__":
    main()
