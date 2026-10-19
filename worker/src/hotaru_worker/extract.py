import json
import logging
from datetime import datetime, timezone

import trafilatura

logger = logging.getLogger(__name__)

MIN_BODY_CHARS = 200


def extract_article(html: str, fallback_url: str) -> dict | None:
    """Полный текст + метаданные страницы через trafilatura.

    Возвращает None, если извлечь не удалось или текста слишком мало —
    в этом случае вызывающий код должен сохранить статью в статусе 'partial'
    (только заголовок/лид из RSS), а не выдумывать текст.
    """
    try:
        raw = trafilatura.extract(
            html,
            url=fallback_url,
            with_metadata=True,
            output_format="json",
            favor_precision=True,
        )
    except Exception:
        logger.exception("trafilatura упала на %s", fallback_url)
        return None

    if not raw:
        return None

    data = json.loads(raw)
    text = (data.get("text") or "").strip()
    if len(text) < MIN_BODY_CHARS:
        return None

    return {
        "title": (data.get("title") or "").strip() or None,
        "body": text,
        "canonical_url": data.get("url") or fallback_url,
        "published_at": _parse_date(data.get("date")),
    }


def _parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
