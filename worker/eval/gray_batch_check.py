"""Проверка качества пакетной классификации серой зоны (фаза 5.4 плана P0).

Берёт N пар с уже сохранёнными вердиктами из gray_zone_verdicts, прогоняет их через
gray_zone.classify_batch пакетами по K пар и сравнивает с сохранёнными. Критерий из плана:
согласие не ниже 90% и ни одной замены different -> same (ложная склейка разных событий —
самая дорогая ошибка). Тратит квоту LLM: N/K запросов. БД только читается.

Запуск: uv run python eval/gray_batch_check.py --pairs 200 --batch-size 12
"""

import argparse
import random
from collections import Counter

from google import genai
from google.genai import types

from hotaru_worker import config, db, gray_zone


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    cfg = config.load()
    with db.connect(cfg.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            select v.verdict, a.title ta, a.lead la, b.title tb, b.lead lb
            from gray_zone_verdicts v
            join articles a on a.id = v.article_a_id
            join articles b on b.id = v.article_b_id
            """
        )
        rows = cur.fetchall()
    print(f"в кеше {len(rows)} пар; вердикты: {dict(Counter(r['verdict'] for r in rows))}")

    # Поровну каждого вердикта: в кеше 55% different, и равномерная выборка почти не
    # проверяет самое опасное (different -> same) и теряет редкий same.
    rng = random.Random(args.seed)
    by_verdict: dict[str, list] = {}
    for row in rows:
        by_verdict.setdefault(row["verdict"], []).append(row)
    per_verdict = args.pairs // len(by_verdict)
    rows = [r for group in by_verdict.values() for r in rng.sample(group, min(per_verdict, len(group)))]
    rng.shuffle(rows)

    client = genai.Client(http_options=types.HttpOptions(timeout=60_000))
    confusion: Counter = Counter()
    unanswered = 0
    requests = 0
    for start in range(0, len(rows), args.batch_size):
        chunk = rows[start : start + args.batch_size]
        pairs = [({"title": r["ta"], "lead": r["la"]}, {"title": r["tb"], "lead": r["lb"]}) for r in chunk]
        try:
            verdicts = gray_zone.classify_batch(client, pairs)
        except gray_zone.DailyQuotaExceeded as exc:
            print(f"квота исчерпана на пакете {start // args.batch_size + 1}: {exc}")
            break
        requests += 1
        for row, verdict in zip(chunk, verdicts):
            if verdict is None:
                unanswered += 1
                continue
            confusion[(row["verdict"], verdict.value)] += 1

    answered = sum(confusion.values())
    if not answered:
        print("ответов нет — сравнивать нечего")
        return
    agree = sum(n for (old, new), n in confusion.items() if old == new)
    print(f"\nзапросов: {requests}, пар отправлено: {answered + unanswered}, без ответа: {unanswered}")
    print(f"согласие с сохранёнными вердиктами: {agree}/{answered} = {agree / answered:.1%}")
    print("сохранённый -> пакетный:")
    for (old, new), n in sorted(confusion.items()):
        print(f"  {old:9} -> {new:9} {n}")
    dangerous = confusion[("different", "same")]
    print(f"\nзамен different -> same: {dangerous}")
    print(f"also related -> same: {confusion[('related', 'same')]}, same -> different: {confusion[('same', 'different')]}")
    ok = agree / answered >= 0.9 and dangerous == 0
    print("КРИТЕРИЙ ПЛАНА (согласие >= 90% и 0 замен different->same):", "ВЫПОЛНЕН" if ok else "НЕ выполнен")


if __name__ == "__main__":
    main()
