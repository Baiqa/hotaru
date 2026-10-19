import html as html_module
import logging
import re
from datetime import datetime, timezone

import feedparser

from . import config, db, extract, normalize
from .politeness import PoliteClient

logger = logging.getLogger("hotaru_worker.collect")

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_summary(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _TAG_RE.sub(" ", raw)
    text = html_module.unescape(text)
    text = " ".join(text.split())
    return text or None


def _entry_published_at(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def _build_article(entry, canonical_url: str, source_id: str, extracted: dict | None) -> dict | None:
    lead = _clean_summary(entry.get("summary"))

    if extracted:
        title = extracted["title"] or _clean_summary(entry.get("title"))
        if not title:
            return None
        return {
            "source_id": source_id,
            "url": entry.get("link"),
            "canonical_url": extracted["canonical_url"],
            "title": title,
            "lead": lead,
            "body": extracted["body"],
            "published_at": extracted["published_at"] or _entry_published_at(entry),
            "content_hash": normalize.content_hash(extracted["body"]),
            "fetch_status": "ok",
        }

    # Полный текст не получен (robots.txt, ошибка сети, слишком короткий текст) —
    # сохраняем только то, что реально есть в RSS, без выдумывания недостающего.
    title = _clean_summary(entry.get("title"))
    if not title or not lead:
        return None

    return {
        "source_id": source_id,
        "url": entry.get("link"),
        "canonical_url": canonical_url,
        "title": title,
        "lead": lead,
        "body": None,
        "published_at": _entry_published_at(entry),
        "content_hash": normalize.content_hash(lead),
        "fetch_status": "partial",
    }


def run_once() -> dict:
    cfg = config.load()
    sources = config.load_sources(cfg.sources_file)
    client = PoliteClient(cfg.user_agent, cfg.request_delay_seconds, cfg.http_timeout_seconds)

    stats = {"sources": 0, "entries_seen": 0, "inserted": 0, "duplicate": 0, "skipped": 0, "failed": 0}

    try:
        with db.connect(cfg.database_url) as conn:
            source_ids = db.seed_sources(conn, sources)

            for src in sources:
                stats["sources"] += 1
                source_id = source_ids[src["base_url"]]
                logger.info("собираю %s", src["name"])

                feed_text = client.get_text(src["rss_url"])
                db.mark_robots_checked(conn, source_id)
                if feed_text is None:
                    logger.warning("не удалось получить ленту %s, пропускаю источник", src["name"])
                    continue

                parsed = feedparser.parse(feed_text)
                if parsed.bozo:
                    logger.warning("проблема разбора ленты %s: %s", src["name"], parsed.bozo_exception)

                entries_with_urls = []
                for entry in parsed.entries:
                    link = entry.get("link")
                    if not link:
                        continue
                    entries_with_urls.append((entry, normalize.normalize_url(link)))

                known = db.existing_canonical_urls(conn, [u for _, u in entries_with_urls])

                for entry, canonical_url in entries_with_urls:
                    stats["entries_seen"] += 1
                    if canonical_url in known:
                        stats["duplicate"] += 1
                        continue

                    article_html = client.get_text(canonical_url)
                    extracted = extract.extract_article(article_html, canonical_url) if article_html else None
                    if extracted:
                        extracted["canonical_url"] = normalize.normalize_url(extracted["canonical_url"])

                    article = _build_article(entry, canonical_url, source_id, extracted)
                    if article is None:
                        stats["skipped"] += 1
                        continue

                    try:
                        outcome = db.insert_article(conn, article)
                        stats[outcome] += 1
                    except Exception:
                        logger.exception("не удалось вставить статью %s", canonical_url)
                        stats["failed"] += 1
    finally:
        client.close()

    logger.info("прогон завершён: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_once()


if __name__ == "__main__":
    main()
