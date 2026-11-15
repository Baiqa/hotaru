"""Декодирование HTML без charset в заголовке (sport-interfax.ru, 2026-11-15)."""

import httpx

from hotaru_worker.politeness import PoliteClient, decode_html

TITLE = "Россияне Демурчян, Олифиренко и Суров вышли в финал ЧЕ по боксу"


def page(charset_meta: str, encoding: str, title: str = TITLE) -> bytes:
    html = f'<html><head><meta http-equiv="Content-Type" content="text/html; {charset_meta}" /><title>{title}</title></head></html>'
    return html.encode(encoding)


def test_windows_1251_declared_only_in_meta_is_decoded_correctly():
    assert TITLE in decode_html(page("charset=windows-1251", "cp1251"))


def test_html5_meta_charset_form():
    html = f'<html><head><meta charset="windows-1251"><title>{TITLE}</title>'.encode("cp1251")
    assert TITLE in decode_html(html)


def test_uppercase_and_alias_names():
    assert TITLE in decode_html(page("charset=WINDOWS-1251", "cp1251"))
    assert TITLE in decode_html(page("charset=cp1251", "cp1251"))


def test_utf8_without_meta_is_decoded_strictly():
    assert TITLE in decode_html(f"<html><title>{TITLE}</title></html>".encode("utf-8"))


def test_utf8_declared_in_meta():
    assert TITLE in decode_html(page("charset=utf-8", "utf-8"))


def test_unknown_charset_name_is_ignored_and_does_not_crash():
    html = page("charset=no-such-charset", "utf-8")
    assert TITLE in decode_html(html)


def test_undecodable_bytes_are_replaced_not_raised():
    out = decode_html(b"<html>\xff\xfe\xfa text</html>")
    assert "text" in out and "�" in out


def test_meta_beyond_first_4kb_is_not_used():
    # Мета-тег ищется только в начале документа; дальше — это уже тело страницы.
    body = b"<html><head>" + b" " * 5000 + b'<meta charset="windows-1251"></head>' + "Привет".encode("utf-8")
    assert "Привет" in decode_html(body)


# --- get_text: путь через настоящий httpx-ответ ------------------------------


def _client_with(handler) -> PoliteClient:
    client = PoliteClient("test-agent", 0.0, 5.0)
    client._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    client._robots["example.org"] = _AllowAll()
    return client


class _AllowAll:
    def can_fetch(self, agent, url):
        return True


def test_get_text_decodes_html_without_header_charset():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=page("charset=windows-1251", "cp1251"))

    assert TITLE in _client_with(handler).get_text("https://example.org/a")


def test_get_text_respects_charset_from_header():
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=windows-1251"}, content=TITLE.encode("cp1251")
        )

    assert _client_with(handler).get_text("https://example.org/a") == TITLE


def test_get_text_leaves_xml_feeds_to_httpx():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/xml"}, content="<rss>Привет</rss>".encode("utf-8"))

    assert _client_with(handler).get_text("https://example.org/feed") == "<rss>Привет</rss>"
