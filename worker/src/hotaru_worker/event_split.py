import logging

from . import config, db, runlog

logger = logging.getLogger("hotaru_worker.event_split")

# Раздел 3.3 CLAUDE.md: "Раз в сутки проверять внутреннюю связность кластера и
# резать, если он распался на две плотные группы". Эмбеддинги уже нормализованы
# (L2-норма ≈ 1, см. подзадачу "Эмбеддинги"), поэтому косинус = скалярное
# произведение. Порог 0.65 — по данным подзадачи "Эмбеддинги": несвязанные пары
# статей там давали косинус ~0.59-0.60, пары об одном сюжете — 0.88-0.97. Самое
# слабое необходимое ребро внутри события ниже 0.65 — признак, что это не одна
# плотная группа.
SPLIT_SIMILARITY_THRESHOLD = 0.65
MIN_EVENT_SIZE_TO_CHECK = 3


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _mst_edges(ids: list[str], vectors: dict[str, list[float]]) -> list[tuple[str, str, float]]:
    """Прим, O(n^2) — число статей в событии мало, простого алгоритма достаточно."""
    in_tree = {ids[0]}
    remaining = set(ids[1:])
    edges = []
    while remaining:
        best = None
        for u in in_tree:
            for v in remaining:
                distance = 1 - _cosine(vectors[u], vectors[v])
                if best is None or distance < best[2]:
                    best = (u, v, distance)
        edges.append(best)
        in_tree.add(best[1])
        remaining.discard(best[1])
    return edges


def _split_by_edge(ids: list[str], mst_edges: list[tuple[str, str, float]], cut: tuple[str, str, float]) -> tuple[set[str], set[str]]:
    adjacency: dict[str, list[str]] = {i: [] for i in ids}
    for u, v, _ in mst_edges:
        if (u, v) == (cut[0], cut[1]):
            continue
        adjacency[u].append(v)
        adjacency[v].append(u)

    visited: set[str] = set()
    stack = [cut[0]]
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        stack.extend(adjacency[node])
    return visited, set(ids) - visited


def _maybe_split_event(conn, event_id: str) -> bool:
    members = db.event_member_embeddings(conn, event_id)
    if len(members) < MIN_EVENT_SIZE_TO_CHECK:
        return False

    vectors = {m["article_id"]: m["embedding"].to_list() for m in members}
    ids = list(vectors.keys())
    mst_edges = _mst_edges(ids, vectors)

    weakest = max(mst_edges, key=lambda e: e[2])
    if (1 - weakest[2]) >= SPLIT_SIMILARITY_THRESHOLD:
        return False

    group_a, group_b = _split_by_edge(ids, mst_edges, weakest)
    smaller = group_a if len(group_a) <= len(group_b) else group_b
    by_id = {m["article_id"]: m for m in members}
    founder = min(smaller, key=lambda aid: by_id[aid]["published_at"] or by_id[aid]["title"])

    new_event_id = db.create_event_from_articles(
        conn, title=by_id[founder]["title"], started_at=by_id[founder]["published_at"], article_ids=list(smaller)
    )
    db.recompute_event_centroid(conn, new_event_id)
    db.recompute_event_centroid(conn, event_id)
    logger.info(
        "разделение: событие %s -> выделено %s статей в новое событие %s (самое слабое ребро similarity=%.3f)",
        event_id, len(smaller), new_event_id, 1 - weakest[2],
    )
    return True


def run_once(conn) -> dict:
    events = db.list_active_events(conn)
    stats = {"checked": 0, "split": 0}
    for event in events:
        if runlog.out_of_time():
            break
        if event["article_count"] < MIN_EVENT_SIZE_TO_CHECK:
            continue
        stats["checked"] += 1
        if _maybe_split_event(conn, event["id"]):
            stats["split"] += 1

    logger.info("разделение событий завершено: %s", stats)
    return stats


def _run() -> dict:
    cfg = config.load()
    with db.connect(cfg.database_url) as conn:
        return run_once(conn)


def main() -> None:
    runlog.run("split-events", _run)


if __name__ == "__main__":
    main()
