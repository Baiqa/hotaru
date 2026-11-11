-- Аудит 2026-11-11, фаза 1 плана P0 (tmp/plans/p0-ispravleniya-posle-audita.md).
-- Раздел 3.1 CLAUDE.md: "полный текст хранится только для внутренней обработки".
-- До этой миграции политика "articles are publicly readable" (using (true)) вместе с
-- табличным grant'ом Supabase по умолчанию отдавала анониму ВСЕ колонки, включая
-- articles.body (проверено запросом с публикуемым ключом: 8109 символов текста).
--
-- RLS ограничивает строки, а не колонки, поэтому политику не трогаем, а сужаем
-- сам grant: табличный select отзываем и выдаём только те колонки, которые читает
-- фронт (web/src/lib/*.ts) либо которые нужны PostgREST для join'ов (id, source_id,
-- article_id). Воркер ходит под владельцем таблиц — его это не затрагивает.

revoke select on public.articles from anon, authenticated;
grant select (
  id, source_id, url, canonical_url, title, lead, published_at, fetch_status, created_at
) on public.articles to anon, authenticated;

-- claims.embedding фронту не нужен (768 чисел на строку), text/evidence — нужны.
revoke select on public.claims from anon, authenticated;
grant select (
  id, article_id, text, type, evidence, created_at
) on public.claims to anon, authenticated;
