import codecs
import logging
import re
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)

_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE)


def decode_html(content: bytes) -> str:
    """Декодирование HTML, когда заголовок Content-Type не называет кодировку.

    httpx в этом случае читает ответ как UTF-8 с заменой нечитаемых байт на «�».
    Проверено 2026-11-15: sport-interfax.ru отдаёт `Content-Type: text/html` без
    charset, а кодировку (windows-1251) объявляет только мета-тегом — все 38 его
    статей в БД с битыми заголовком и текстом. Поэтому сначала ищем мета-тег в
    начале документа, затем пробуем строгий UTF-8, и только потом — замену.
    """
    match = _META_CHARSET_RE.search(content[:4096])
    if match:
        try:
            return content.decode(codecs.lookup(match.group(1).decode("ascii")).name, errors="replace")
        except LookupError:
            pass  # неизвестное имя кодировки — как будто мета-тега нет
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("utf-8", errors="replace")


class PoliteClient:
    """HTTP-клиент, соблюдающий robots.txt и паузу между запросами к одному домену.

    Раздел 3.1 CLAUDE.md, "Правила вежливости (обязательны)".
    """

    def __init__(self, user_agent: str, min_delay: float, timeout: float):
        self._user_agent = user_agent
        self._min_delay = min_delay
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request_at: dict[str, float] = {}

    def _robots_parser(self, netloc: str, scheme: str) -> RobotFileParser:
        if netloc not in self._robots:
            parser = RobotFileParser()
            robots_url = f"{scheme}://{netloc}/robots.txt"
            try:
                resp = self._client.get(robots_url)
                if resp.status_code < 400:
                    parser.parse(resp.text.splitlines())
                else:
                    parser.parse([])  # нет robots.txt -> разрешено всё
            except httpx.HTTPError:
                logger.warning("robots.txt для %s недоступен, считаем разрешённым", netloc)
                parser.parse([])
            self._robots[netloc] = parser
        return self._robots[netloc]

    def _wait_for_slot(self, netloc: str) -> None:
        last = self._last_request_at.get(netloc)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self._min_delay:
                time.sleep(self._min_delay - elapsed)
        self._last_request_at[netloc] = time.monotonic()

    def get_text(self, url: str) -> str | None:
        parts = urlsplit(url)
        netloc = parts.netloc
        robots = self._robots_parser(netloc, parts.scheme or "https")
        if not robots.can_fetch(self._user_agent, url):
            logger.info("robots.txt запрещает загрузку %s", url)
            return None

        self._wait_for_slot(netloc)
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("запрос к %s не удался: %s", url, exc)
            return None
        if resp.status_code >= 400:
            logger.warning("запрос к %s вернул статус %s", url, resp.status_code)
            return None
        if resp.charset_encoding is None and "html" in resp.headers.get("content-type", "").lower():
            return decode_html(resp.content)
        return resp.text

    def close(self) -> None:
        self._client.close()
