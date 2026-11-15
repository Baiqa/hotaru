"""Сравнение скоринга пар статей на размеченной выборке (фаза 4 плана P0). БД только читается.

Вход: eval/labels.json (разметка «одно событие / разные») и jsonl от eval/entities_dryrun.py
(упоминания по новому entities.py). Для всех пар размеченных статей в окне 5 суток считает
скор четырьмя способами:
  A  старые сущности из БД + обычный Жаккар            (то, что работало до фазы 4)
  A+ старые сущности из БД + IDF-Жаккар                 (эффект одного только IDF)
  B  новые сущности + обычный Жаккар                    (эффект одной только нормализации)
  C  новые сущности + IDF-Жаккар                        (то, что делает фаза 4)
и сравнивает: ROC-AUC (все пары и «трудные» пары с косинусом >= 0.55), долю статей, у
которых верная пара попала в топ-2 (CANDIDATE_TRIES), и разбиение пар топ-2 по порогам.

Запуск: uv run python eval/scoring_auc.py --dryrun dryrun.jsonl
"""

import argparse
import functools
import itertools
import json
import uuid
from collections import defaultdict
from pathlib import Path

import numpy as np
from rapidfuzz import fuzz, process

from hotaru_worker import clustering, config, db, scoring

EVAL_DIR = Path(__file__).resolve().parent

# score_pair на каждую пару заново разбирает тексты регулярками; на ~100 тыс. пар × 4 варианта
# это минуты. Тексты повторяются, поэтому результат кешируем (значения не меняются).
scoring.extract_number_date_tokens = functools.lru_cache(maxsize=None)(scoring.extract_number_date_tokens)
HARD_COSINE = 0.55


def auc(scores: list[float], labels: list[int]) -> float:
    """ROC-AUC через ранги (Манн-Уитни), со средним рангом для равных скоров."""
    order = np.argsort(scores, kind="mergesort")
    sorted_scores = np.asarray(scores)[order]
    ranks = np.empty(len(scores))
    i = 0
    while i < len(scores):
        j = i
        while j + 1 < len(scores) and sorted_scores[j + 1] == sorted_scores[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2 + 1
        i = j + 1
    labels_arr = np.asarray(labels)
    n_pos = int(labels_arr.sum())
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return (ranks[labels_arr == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def resolve_names(records: list[dict]) -> dict[str, set[str]]:
    """Имя сущности -> ключ, как это делает entities.resolve_entity: точное совпадение без
    регистра, затем WRatio >= порога среди уже виденных имён того же типа."""
    exact: dict[tuple[str, str], str] = {}
    seen: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    per_article: dict[str, set[str]] = {}
    for rec in records:
        keys: set[str] = set()
        for name, etype, _count in rec["names"]:
            ident = (rec["lang"], name.lower())
            if ident in exact:
                keys.add(exact[ident])
                continue
            names, key_list = seen.setdefault((rec["lang"], etype), ([], []))
            match = process.extractOne(name, names, scorer=fuzz.WRatio, score_cutoff=92) if names else None
            key = key_list[match[2]] if match else f"{rec['lang']}:{etype}:{name.lower()}"
            exact[ident] = key
            names.append(name)
            key_list.append(key)
            keys.add(key)
        per_article[rec["id"]] = keys
    return per_article


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dryrun", required=True)
    args = parser.parse_args()

    labels = json.loads((EVAL_DIR / "labels.json").read_text(encoding="utf-8"))
    truth = {a: k for k, c in labels["clusters"].items() for a in c["articleIds"]}
    ids = sorted(truth)

    records = [json.loads(line) for line in Path(args.dryrun).read_text(encoding="utf-8").splitlines() if line]
    new_entities = resolve_names(records)
    # df для варианта C — по всему корпусу dry-run (окно 7 суток + размеченные)
    new_df: dict[str, int] = defaultdict(int)
    for keys in new_entities.values():
        for key in keys:
            new_df[key] += 1
    new_weights = scoring.EntityWeights(len(new_entities), dict(new_df))

    cfg = config.load()
    with db.connect(cfg.database_url) as conn:
        old_entities = db.entity_ids_by_article(conn, [uuid.UUID(a) for a in ids])
        total, old_df = db.entity_document_frequencies(conn)
        old_weights = scoring.EntityWeights(total, old_df)
        with conn.cursor() as cur:
            cur.execute(
                "select id, title, lead, body, published_at, embedding from articles where id = any(%s::uuid[])", (ids,)
            )
            rows = {str(r["id"]): r for r in cur.fetchall()}
    old_entities = {str(k): {str(e) for e in v} for k, v in old_entities.items()}
    old_weights = scoring.EntityWeights(old_weights.total_articles, {str(k): v for k, v in old_weights.doc_freq.items()})

    missing = [a for a in ids if a not in new_entities]
    print(f"размечено {len(ids)}, нет в dry-run {len(missing)} (исключены)")
    ids = [a for a in ids if a in new_entities and a in rows]
    vec = {a: np.asarray(rows[a]["embedding"].to_list()) for a in ids}
    for a in ids:
        vec[a] = vec[a] / np.linalg.norm(vec[a])

    variants = {
        "A  старые+Жаккар": lambda a, b, art_a, art_b, cos: scoring.score_pair(art_a, old_entities[a], art_b, old_entities[b], cos),
        "A+ старые+IDF": lambda a, b, art_a, art_b, cos: scoring.score_pair(art_a, old_entities[a], art_b, old_entities[b], cos, old_weights),
        "B  новые+Жаккар": lambda a, b, art_a, art_b, cos: scoring.score_pair(art_a, new_entities[a], art_b, new_entities[b], cos),
        "C  новые+IDF": lambda a, b, art_a, art_b, cos: scoring.score_pair(art_a, new_entities[a], art_b, new_entities[b], cos, new_weights),
    }

    window = scoring.DEFAULT_WINDOW_DAYS * 86400
    pairs = []
    for a, b in itertools.combinations(ids, 2):
        pa, pb = rows[a]["published_at"], rows[b]["published_at"]
        if abs((pa - pb).total_seconds()) > window:
            continue
        pairs.append((a, b, int(truth[a] == truth[b]), float(vec[a] @ vec[b])))
    n_pos = sum(p[2] for p in pairs)
    print(f"пар в окне {scoring.DEFAULT_WINDOW_DAYS} сут: {len(pairs)}, из них «одно событие»: {n_pos}")

    lower, upper = clustering.LOWER_THRESHOLD, clustering.UPPER_THRESHOLD
    results = {}
    for name, fn in variants.items():
        scores = {}
        for a, b, _y, cos in pairs:
            scores[(a, b)] = fn(a, b, rows[a], rows[b], cos)["score"]
        all_auc = auc([scores[(a, b)] for a, b, _, _ in pairs], [y for _, _, y, _ in pairs])
        hard = [(a, b, y) for a, b, y, cos in pairs if cos >= HARD_COSINE]
        hard_auc = auc([scores[(a, b)] for a, b, _ in hard], [y for _, _, y in hard])

        partners = defaultdict(list)
        for a, b, y, _ in pairs:
            partners[a].append((scores[(a, b)], b, y))
            partners[b].append((scores[(a, b)], a, y))
        with_truth = [a for a in ids if any(y for _, _, y in partners[a])]
        hits = sum(1 for a in with_truth if any(y for _, _, y in sorted(partners[a], reverse=True)[: clustering.CANDIDATE_TRIES]))
        top = [t for a in ids for t in sorted(partners[a], reverse=True)[: clustering.CANDIDATE_TRIES]]
        gray = sum(1 for s, _, _ in top if lower <= s <= upper) / len(top)
        auto_join = [(s, y) for s, _, y in top if s > upper]
        auto_join_prec = sum(y for _, y in auto_join) / len(auto_join) if auto_join else float("nan")
        true_pairs = [(scores[(a, b)]) for a, b, y, _ in pairs if y]
        below = sum(1 for s in true_pairs if s < lower) / len(true_pairs)
        results[name] = (all_auc, hard_auc, hits, len(with_truth), gray, len(auto_join), auto_join_prec, below, len(hard))

    print()
    print(f"{'вариант':22} {'AUC все':>8} {'AUC труд.':>9} {'топ-2 hit':>10} {'серая зона':>10} {'авто-join':>10} {'их точн.':>8} {'верные <0.38':>12}")
    for name, (all_auc, hard_auc, hits, n_t, gray, n_auto, prec, below, _n_hard) in results.items():
        print(f"{name:22} {all_auc:8.4f} {hard_auc:9.4f} {hits:5}/{n_t:<4} {gray:10.1%} {n_auto:10} {prec:8.1%} {below:12.1%}")
    print(f"(трудные пары: косинус >= {HARD_COSINE}, их {results[next(iter(results))][-1]})")


if __name__ == "__main__":
    main()
