import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Раздел 3.1 CLAUDE.md: "Нормализация URL: убрать UTM-метки, взять canonical."
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "yclid", "ysclid", "mc_cid", "mc_eid",
}


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    query_pairs = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    )
    query = urlencode(query_pairs)
    return urlunsplit((scheme, netloc, path, query, ""))


def content_hash(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
