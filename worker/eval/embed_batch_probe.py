"""Эксперимент 5.5 плана P0: считается ли запрос эмбеддингов с несколькими текстами за один.

Квота эмбеддингов free-tier — 1000 в сутки, а одних статей приходит ~800. Если пакет из N
текстов считается за ОДИН запрос, оба шага эмбеддингов (статьи и факты) переводятся на пакеты
и квоты хватает на всё. Если за N — не переводятся.

Как ставить опыт: запускать СРАЗУ после сброса дневной квоты (около 08:00 UTC), пока шаги
конвейера её не потратили. Скрипт берёт статьи без эмбеддинга, считает их пакетами по --batch-size
и СОХРАНЯЕТ результат (опыт не пропадает впустую), пока не упрётся в дневной лимит или не наберёт
--max-texts. Вывод:
  - обработано >> 1000 текстов без 429  -> пакет считается за один запрос (или за долю);
  - 429 около 1000 текстов              -> каждый текст считается отдельно, пакеты квоту не экономят.

Запуск: uv run python eval/embed_batch_probe.py --batch-size 20 --max-texts 1500
"""

import argparse

from google.genai import errors

from hotaru_worker import config, db, embeddings, quota


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-texts", type=int, default=1500)
    args = parser.parse_args()

    cfg = config.load()
    client = embeddings.build_client()
    texts_done = requests_done = 0
    verdict = f"лимит не достигнут за {args.max_texts} текстов"
    while texts_done < args.max_texts:
        with db.connect(cfg.database_url) as conn:
            batch = db.articles_missing_embedding(conn, args.batch_size)
            if not batch:
                verdict = "статьи без эмбеддингов кончились раньше, чем лимит"
                break
            try:
                vectors = embeddings.embed_texts(client, [embeddings.article_text(a) for a in batch])
            except errors.APIError as exc:
                if not quota.is_daily_quota(exc):
                    raise
                verdict = f"дневной лимит на {texts_done} текстах ({requests_done} запросов)"
                break
            for article, vector in zip(batch, vectors):
                db.update_embedding(conn, article["id"], vector)
        texts_done += len(batch)
        requests_done += 1
        if requests_done % 10 == 0:
            print(f"{texts_done} текстов, {requests_done} запросов", flush=True)

    print(f"\nитог: {texts_done} текстов, {requests_done} запросов; {verdict}")
    print("ВЫВОД:", "пакет экономит квоту (текстов заметно больше 1000)" if texts_done > 1100 else "пакет НЕ экономит квоту (упёрлись около 1000 текстов) или опыт не дошёл до лимита")


if __name__ == "__main__":
    main()
