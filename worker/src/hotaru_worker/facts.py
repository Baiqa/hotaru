"""Факты события для синтеза текста: группы вместо сырых утверждений (фаза 5 плана P0).

Раньше в модель уходили все сырые факты события — до 267 строк, около 90% из которых
повторяют друг друга (одно и то же сообщили несколько изданий). Теперь одна строка — одна
группа фактов (claim_groups) со списком изданий, максимум MAX_FACT_LINES строк.
У одиночных событий групп нет (внутри одной статьи группировать нечего), поэтому каждое
сырое утверждение — отдельная «группа из одного».
"""

MAX_FACT_LINES = 40


def collapse_facts(rows: list[dict], limit: int = MAX_FACT_LINES) -> list[dict]:
    """rows — утверждения события: claim_id, text, evidence, type, created_at,
    source_name, source_url, group_id, bucket, source_count, relation (поля группы
    пустые, если утверждение ещё не сгруппировано). Возвращает не больше limit фактов
    в том же формате, что раньше отдавал event_facts_for_synthesis (кроме numbers —
    их добавляет вызывающий код по claim_id).

    Порядок важности при обрезке: спорные группы, затем подтверждённые несколькими
    изданиями (больше изданий — выше), затем упомянутые одним; внутри — по времени.
    """
    groups: dict[object, list[dict]] = {}
    for row in sorted(rows, key=lambda r: r["created_at"]):
        key = row["group_id"] if row["group_id"] is not None else ("solo", row["claim_id"])
        groups.setdefault(key, []).append(row)

    ranked: list[tuple[tuple, dict]] = []
    for members in groups.values():
        first = members[0]
        bucket = first["bucket"] or "unique"
        sources: list[str] = []
        for member in members:
            if member["source_name"] not in sources:
                sources.append(member["source_name"])
        source_count = first["source_count"] or len(sources)
        # Представитель — самое раннее утверждение, не опровергающее группу.
        representative = next((m for m in members if m["relation"] != "contradicts"), first)

        facts = [
            {
                "claim_id": representative["claim_id"],
                "text": representative["text"],
                # Для проверки на дословное копирование (synthesis._copies_original) нужны
                # цитаты ВСЕХ утверждений группы, а не только показанного.
                "evidence": "\n".join(m["evidence"] for m in members),
                "type": representative["type"],
                "source_name": ", ".join(sources),
                "source_url": representative["source_url"],
                "bucket": bucket,
                "source_count": source_count,
            }
        ]
        if bucket == "disputed":
            # Спорная группа бесполезна без обеих сторон: опровергающие утверждения — отдельными строками.
            for member in members:
                if member["relation"] == "contradicts":
                    facts.append(
                        {
                            "claim_id": member["claim_id"],
                            "text": member["text"],
                            "evidence": member["evidence"],
                            "type": member["type"],
                            "source_name": member["source_name"],
                            "source_url": member["source_url"],
                            "bucket": "disputed",
                            "source_count": source_count,
                        }
                    )
        rank = (bucket != "disputed", -source_count, first["created_at"])
        ranked.extend((rank, fact) for fact in facts)

    ranked.sort(key=lambda item: item[0])
    return [fact for _, fact in ranked[:limit]]
