import logging

from google import genai

from . import db, gray_zone, runlog, scoring

logger = logging.getLogger(__name__)

# Раздел 3.3 CLAUDE.md: "> 0.75 → присоединить", "< 0.45 → новое событие".
# Нижний порог снижен до 0.40 по факту с реальных данных (тайфун Dujuan, разбор
# сигналов — см. tmp/plans/etap1-porogi-events.md), затем до 0.38 по B-Cubed разметке
# (см. tmp/plans/etap1-kalibrovka-po-keisam.md, находка 3) — та же логика точечной
# правки, но границы проверены: 0.38 чинит только случаи, где верная пара УЖЕ входит в
# топ по рангу (как находка 2) и лишь чуть недотягивает до порога. НЕ чинит ДТ-экспорт/
# резолюцию ООН из того же расследования — там верная пара на ранге #17-19, глубже
# CANDIDATE_TRIES, задавлена частыми сущностями (США/МИД/Россия); это открытая, не
# точечная проблема (нужен IDF-вес сущностей или подобное), осознанно не чиним здесь.
UPPER_THRESHOLD = 0.75
LOWER_THRESHOLD = 0.38

# Находка 2 (см. tmp/plans/etap1-kalibrovka-po-keisam.md): сравнение только с единственным
# лучшим кандидатом хрупко — на реальных данных верная пара (score 0.476, gray-zone)
# проигрывала по score несвязанной статье (0.491) из-за случайного совпадения общих
# сущностей, и до LLM никогда не доходила. Пробуем до CANDIDATE_TRIES кандидатов по очереди.
# 2026-11-10: было 3. Серая зона занимала 87% статей (монитор), квоты Gemini/Groq не
# вывозят. Офлайн-расчёт на 250 размеченных статьях (tmp/plans/seraya-zona-nagruzka.md):
# 3 -> 2 даёт -22% вызовов LLM (1.65 -> 1.28 на статью) ценой ~2 из 126 соединяемых.
CANDIDATE_TRIES = 2


def _resolve_join_event(conn, candidate_article: dict) -> str:
    """Событие кандидата, если оно уже есть. Кандидат мог быть опубликован позже
    целевой статьи (окно по времени симметричное) и ещё не дойти в очереди —
    тогда заводим новое событие и сразу присоединяем к нему обе статьи. Это
    самовосстанавливающееся поведение (не костыль): несовершенство порядка
    обработки — ровно то, что чинит будущая фоновая задача слияния."""
    event_id = db.event_id_for_article(conn, candidate_article["id"])
    if event_id is None:
        # candidate_article — из scoring.score_candidates(), там нет embedding
        # (не выбирали в SQL кандидатов); create_event() он нужен, дозапрашиваем.
        full_candidate = db.get_article(conn, candidate_article["id"])
        event_id = db.create_event(conn, full_candidate)
    return event_id


def _fits_event_limits(conn, article: dict, candidate_article: dict) -> bool:
    """Жёсткий лимит события (scoring.MAX_EVENT_*): не присоединять статью к событию,
    которое после этого стало бы длиннее 72 часов или больше 40 статей. Проверяется ДО
    обращения к LLM, чтобы не тратить квоту на пару, которая всё равно не склеится.
    Если у кандидата события ещё нет, оно будет создано из двух статей — тогда
    проверяется размах между ними."""
    event_id = db.event_id_for_article(conn, candidate_article["id"])
    if event_id is None:
        count, first_at, last_at = 1, candidate_article.get("published_at"), candidate_article.get("published_at")
    else:
        span = db.event_span(conn, event_id)
        count, first_at, last_at = span["article_count"], span["first_at"], span["last_at"]
    published_at = article.get("published_at")
    return scoring.within_event_limits(count, first_at, last_at, 1, published_at, published_at)


def _is_gray(score: float) -> bool:
    return LOWER_THRESHOLD <= score <= UPPER_THRESHOLD


def prefetch_gray_zone(conn, client: genai.Client, articles: list[dict], batch_size: int) -> dict:
    """Фаза 5.4 плана P0. Скоры без LLM для всей пачки статей, затем вердикты серой зоны —
    пакетами по batch_size пар одним запросом (gray_zone.classify_batch) в кеш
    gray_zone_verdicts. Дальше cluster_article работает как прежде, последовательно, и
    находит вердикты в кеше: логика решений не меняется, меняется число запросов.
    Возвращает {article_id: scored} — готовые скоры, чтобы не считать их второй раз.
    Пары берутся в том же порядке и с теми же отсечками (топ CANDIDATE_TRIES, серая зона,
    лимит события), что и в cluster_article. Исключения LLM (квота) пробрасываются."""
    scored_by_article: dict = {}
    pending: list[tuple[dict, dict]] = []
    seen: set[tuple] = set()
    for article in articles:
        if db.event_id_for_article(conn, article["id"]) is not None:
            continue
        scored = scoring.score_candidates(conn, article["id"])
        scored_by_article[article["id"]] = scored
        for result in scored[:CANDIDATE_TRIES]:
            candidate = result["article"]
            if not _is_gray(result["score"]):
                continue
            key = tuple(sorted((str(article["id"]), str(candidate["id"]))))
            if key in seen or db.get_gray_zone_verdict(conn, article["id"], candidate["id"]) is not None:
                continue
            if not _fits_event_limits(conn, article, candidate):
                continue
            seen.add(key)
            pending.append((article, result))

    for start in range(0, len(pending), batch_size):
        if runlog.out_of_time():
            break
        chunk = pending[start : start + batch_size]
        verdicts = gray_zone.classify_batch(client, [(a, r["article"]) for a, r in chunk])
        for (article, result), verdict in zip(chunk, verdicts):
            if verdict is not None:
                db.save_gray_zone_verdict(
                    conn, article["id"], result["article"]["id"], verdict.value, result["score"], result["signals"]
                )
    return scored_by_article


def cluster_article(
    conn, client: genai.Client, article: dict, scored: list[dict] | None = None
) -> tuple[str, bool]:
    """Раздел 3.3 CLAUDE.md, инкрементальный алгоритм на одну статью.
    Возвращает (event_id, created_new_event). scored — уже посчитанные скоры
    (prefetch_gray_zone), иначе считаются здесь."""
    if scored is None:
        scored = scoring.score_candidates(conn, article["id"])

    for candidate_result in scored[:CANDIDATE_TRIES]:
        candidate_article = candidate_result["article"]
        score = candidate_result["score"]

        if score >= LOWER_THRESHOLD and not _fits_event_limits(conn, article, candidate_article):
            continue

        if score > UPPER_THRESHOLD:
            should_join = True
        elif score < LOWER_THRESHOLD:
            should_join = False
        else:
            verdict = db.get_gray_zone_verdict(conn, article["id"], candidate_article["id"])
            if verdict is None:
                verdict = gray_zone.classify(client, article, candidate_article).value
                db.save_gray_zone_verdict(
                    conn, article["id"], candidate_article["id"], verdict, score, candidate_result["signals"]
                )
            should_join = verdict == "same"

        if should_join:
            event_id = _resolve_join_event(conn, candidate_article)
            db.join_event(conn, event_id, article["id"], score)
            db.recompute_event_centroid(conn, event_id)
            return event_id, False

    event_id = db.create_event(conn, article)
    db.recompute_event_centroid(conn, event_id)
    return event_id, True
