import logging

from google import genai

from . import db, gray_zone, scoring

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


def cluster_article(conn, client: genai.Client, article: dict) -> tuple[str, bool]:
    """Раздел 3.3 CLAUDE.md, инкрементальный алгоритм на одну статью.
    Возвращает (event_id, created_new_event)."""
    scored = scoring.score_candidates(conn, article["id"])

    for candidate_result in scored[:CANDIDATE_TRIES]:
        candidate_article = candidate_result["article"]
        score = candidate_result["score"]

        if score > UPPER_THRESHOLD:
            should_join = True
        elif score < LOWER_THRESHOLD:
            should_join = False
        else:
            verdict = db.get_gray_zone_verdict(conn, article["id"], candidate_article["id"])
            if verdict is None:
                verdict = gray_zone.classify(client, article, candidate_article).value
                db.save_gray_zone_verdict(conn, article["id"], candidate_article["id"], verdict)
            should_join = verdict == "same"

        if should_join:
            event_id = _resolve_join_event(conn, candidate_article)
            db.join_event(conn, event_id, article["id"], score)
            db.recompute_event_centroid(conn, event_id)
            return event_id, False

    event_id = db.create_event(conn, article)
    db.recompute_event_centroid(conn, event_id)
    return event_id, True
