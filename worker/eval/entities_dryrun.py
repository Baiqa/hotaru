"""Прогон нового извлечения сущностей БЕЗ записи в БД (фаза 4 плана P0).

Для статей за последние N дней и/или размеченных в eval/labels.json считает
упоминания по текущему коду entities.py и пишет их в jsonl:
    {"id", "lang", "src", "names": [[имя, тип, число упоминаний], ...]}
БД только читается. Нужен, чтобы сравнить старое и новое извлечение (топ сущностей,
скоринг на размеченных парах) до пересборки данных.

Запуск (долго: ~0.5 с на статью):
    uv run python eval/entities_dryrun.py --days 7 --labeled --out dryrun.jsonl
Продолжается с места остановки: id, уже лежащие в out, пропускаются.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from hotaru_worker import config, db, entities

EVAL_DIR = Path(__file__).resolve().parent
PAGE = 40


def _labeled_ids() -> list[str]:
    data = json.loads((EVAL_DIR / "labels.json").read_text(encoding="utf-8"))
    return [a for cluster in data["clusters"].values() for a in cluster["articleIds"]]


def _fetch_ids(cfg, days: int | None, labeled: bool) -> list[str]:
    ids: list[str] = []
    with db.connect(cfg.database_url) as conn, conn.cursor() as cur:
        if days:
            cur.execute(
                """select a.id::text id from articles a
                   where a.published_at > now() - make_interval(days => %s) and a.body is not null
                   order by a.published_at desc""",
                (days,),
            )
            ids += [row["id"] for row in cur.fetchall()]
    if labeled:
        ids += [i for i in _labeled_ids() if i not in set(ids)]
    return ids


def _fetch_articles(cfg, ids: list[str]) -> list[dict]:
    with db.connect(cfg.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """select a.id::text id, a.title, a.lead, a.body, s.lang, s.name src
               from articles a join sources s on s.id = a.source_id
               where a.id = any(%s::uuid[])""",
            (ids,),
        )
        return cur.fetchall()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--labeled", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    cfg = config.load()
    out = Path(args.out)
    done: set[str] = set()
    if out.exists():
        done = {json.loads(line)["id"] for line in out.read_text(encoding="utf-8").splitlines() if line}
    ids = [i for i in _fetch_ids(cfg, args.days, args.labeled) if i not in done]
    print(f"к обработке {len(ids)} статей (уже готово {len(done)})", flush=True)

    models = entities.load_models()
    with db.connect(cfg.database_url) as conn:
        entities.seed_known_person_tokens(db.person_canonical_names(conn))

    with out.open("a", encoding="utf-8") as f:
        for start in range(0, len(ids), PAGE):
            for article in _fetch_articles(cfg, ids[start : start + PAGE]):
                nlp = models.get(article["lang"])
                if nlp is None:
                    continue
                text = "\n".join(p for p in (article["title"], article["lead"], article["body"]) if p)
                counts = Counter(entities.extract_mentions(nlp, text, article["lang"]))
                names = [[name, etype, n] for (name, etype), n in counts.items()]
                f.write(json.dumps({"id": article["id"], "lang": article["lang"], "src": article["src"], "names": names}, ensure_ascii=False) + "\n")
            f.flush()
            print(f"{min(start + PAGE, len(ids))}/{len(ids)}", flush=True)


if __name__ == "__main__":
    main()
