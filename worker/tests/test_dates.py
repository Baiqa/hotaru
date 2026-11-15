"""Выбор времени публикации и заголовка (фаза 3 плана P0)."""

import time
from datetime import datetime, timedelta, timezone

from hotaru_worker import collect, dates

UTC = timezone.utc
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def dt(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --- choose_published_at -----------------------------------------------------


def test_rss_time_wins_over_page_date():
    # Реальный случай: страница отдаёт полночь, лента — точное время.
    rss = dt(2026, 11, 15, 9, 41, 17)
    page = dt(2026, 11, 15, 0, 0, 0)
    assert dates.choose_published_at(rss, page, NOW) == rss


def test_rss_time_wins_even_when_page_date_is_garbage():
    # РИА: страница дала 2023-01-01 для статьи из /20261115/.
    rss = dt(2026, 11, 15, 11, 30)
    assert dates.choose_published_at(rss, dt(2023, 1, 1), NOW) == rss


def test_old_rss_time_is_kept():
    # Guardian законно держит в ленте материалы по 12 дней: это настоящая дата.
    rss = NOW - timedelta(days=12)
    assert dates.choose_published_at(rss, None, NOW) == rss


def test_rss_time_slightly_ahead_of_clock_is_accepted():
    rss = NOW + timedelta(minutes=30)
    assert dates.choose_published_at(rss, None, NOW) == rss


def test_rss_time_in_future_falls_back_to_plausible_page_date():
    page = NOW - timedelta(days=1)
    assert dates.choose_published_at(NOW + timedelta(days=3), page, NOW) == page


def test_rss_time_in_future_and_no_page_date_gives_now():
    assert dates.choose_published_at(NOW + timedelta(days=3), None, NOW) == NOW


def test_no_rss_time_uses_plausible_page_date():
    page = dt(2026, 11, 14)
    assert dates.choose_published_at(None, page, NOW) == page


def test_no_rss_time_and_garbage_page_date_gives_now():
    assert dates.choose_published_at(None, dt(2023, 1, 1), NOW) == NOW
    assert dates.choose_published_at(None, dt(2026, 11, 1), NOW) == NOW  # старше недели


def test_page_date_boundary_is_one_week():
    assert dates.choose_published_at(None, NOW - timedelta(days=7), NOW) == NOW - timedelta(days=7)
    assert dates.choose_published_at(None, NOW - timedelta(days=7, seconds=1), NOW) == NOW


def test_page_date_in_future_is_rejected():
    assert dates.choose_published_at(None, NOW + timedelta(days=2), NOW) == NOW


def test_nothing_known_gives_now():
    assert dates.choose_published_at(None, None, NOW) == NOW


# --- _build_article: заголовок и время целиком --------------------------------


def entry(title="Заголовок из ленты", summary="Лид из ленты", published=(2026, 11, 15, 9, 41, 17), link="https://example.org/a"):
    e = {"title": title, "summary": summary, "link": link}
    if published:
        e["published_parsed"] = time.struct_time((*published, 0, 0, 0))
    return e


def extracted(title="Заголовок со страницы | Site", date=dt(2026, 11, 15)):
    return {"title": title, "body": "Текст статьи. " * 30, "canonical_url": "https://example.org/a", "published_at": date}


def test_ok_article_takes_title_and_time_from_rss():
    art = collect._build_article(entry(), "https://example.org/a", "src-1", extracted(), now=NOW)
    assert art["title"] == "Заголовок из ленты"  # без хвоста «| Site»
    assert art["published_at"] == dt(2026, 11, 15, 9, 41, 17)
    assert art["fetch_status"] == "ok"


def test_ok_article_falls_back_to_page_title_when_rss_title_is_empty():
    art = collect._build_article(entry(title=""), "https://example.org/a", "src-1", extracted(), now=NOW)
    assert art["title"] == "Заголовок со страницы | Site"


def test_ok_article_without_any_title_is_skipped():
    assert collect._build_article(entry(title=""), "https://example.org/a", "src-1", extracted(title=None), now=NOW) is None


def test_html_in_rss_title_is_cleaned():
    art = collect._build_article(entry(title="Цена &quot;выросла&quot; <b>на 5%</b>"), "u", "s", extracted(), now=NOW)
    assert art["title"] == 'Цена "выросла" на 5%'


def test_ok_article_without_rss_time_uses_plausible_page_date():
    art = collect._build_article(entry(published=None), "u", "s", extracted(date=dt(2026, 11, 14)), now=NOW)
    assert art["published_at"] == dt(2026, 11, 14)


def test_partial_article_uses_rss_time_and_title():
    art = collect._build_article(entry(), "https://example.org/a", "src-1", None, now=NOW)
    assert art["fetch_status"] == "partial"
    assert art["body"] is None
    assert art["published_at"] == dt(2026, 11, 15, 9, 41, 17)
    assert art["canonical_url"] == "https://example.org/a"


def test_partial_article_without_rss_time_gets_collection_time():
    art = collect._build_article(entry(published=None), "u", "s", None, now=NOW)
    assert art["published_at"] == NOW


def test_partial_article_without_lead_is_skipped():
    assert collect._build_article(entry(summary=""), "u", "s", None, now=NOW) is None


def test_updated_parsed_is_used_when_published_is_absent():
    e = entry(published=None)
    e["updated_parsed"] = time.struct_time((2026, 11, 15, 8, 0, 0, 0, 0, 0))
    art = collect._build_article(e, "u", "s", None, now=NOW)
    assert art["published_at"] == dt(2026, 11, 15, 8, 0, 0)
