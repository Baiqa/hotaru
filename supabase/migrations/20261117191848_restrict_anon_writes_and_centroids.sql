-- Аудит прав 2026-11-18 (docs/rbac.md, пробелы 1 и 2).
--
-- 1. Supabase по умолчанию выдаёт anon и authenticated на каждую таблицу public полный набор
--    INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER. Запись не проходила только потому, что нет
--    write-политик RLS, а TRUNCATE RLS вообще не ограничивает: защита держалась на одном слое.
--    Фронт только читает, пишет воркер под владельцем таблиц (postgres, обходит RLS) — ему эти
--    гранты не нужны. Отзываем всё, кроме SELECT.
-- 2. Векторы centroid (events, claim_groups, stories) фронту не нужны: он выбирает колонки явно
--    (web/src/lib/*.ts). Так же, как для articles и claims (20261112090000), отзываем табличный
--    SELECT и выдаём список колонок. Новые колонки этих таблиц анону придётся выдавать явно.
-- 3. gray_zone_verdicts и monitoring_checks — служебные таблицы воркера. Как pipeline_runs
--    (20261112120000), закрываем доступ грантами, а не только отсутствием политик RLS.
--
-- admin_reader и service_role не затрагиваются: у admin_reader свои явные гранты и политики
-- (20261117120000), на service_role эта миграция не действует.

-- 1. Запись.
revoke insert, update, delete, truncate, references, trigger
  on all tables in schema public from anon, authenticated;

-- Новые таблицы, созданные ролью postgres (миграции), больше не получают write-гранты автоматически.
-- Таблицы, созданные ролью supabase_admin (например, из панели), по-прежнему получают их по
-- умолчанию: эти настройки принадлежат другой роли и здесь не меняются.
alter default privileges for role postgres in schema public
  revoke insert, update, delete, truncate, references, trigger on tables from anon, authenticated;

-- 2. Центроиды.
revoke select on public.events from anon, authenticated;
grant select (
  id, canonical_id, title, started_at, updated_at, status, created_at,
  last_article_at, source_count, rendered_source_count, render_attempts, render_failed_at
) on public.events to anon, authenticated;

revoke select on public.claim_groups from anon, authenticated;
grant select (
  id, event_id, bucket, source_count, created_at, updated_at
) on public.claim_groups to anon, authenticated;

revoke select on public.stories from anon, authenticated;
grant select (
  id, title, started_at, updated_at, status, created_at
) on public.stories to anon, authenticated;

-- 3. Служебные таблицы.
revoke all on public.gray_zone_verdicts from anon, authenticated;
revoke all on public.monitoring_checks from anon, authenticated;

-- Самопроверка: если права не такие, как задумано, миграция откатывается целиком.
do $$
declare
  bad text;
begin
  select string_agg(c.relname || ':' || p, ', ') into bad
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  cross join unnest(array['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) as p
  cross join unnest(array['anon', 'authenticated']) as r
  where n.nspname = 'public' and c.relkind = 'r'
    and has_table_privilege(r, c.oid, p);
  if bad is not null then
    raise exception 'у anon/authenticated остались write-гранты: %', bad;
  end if;

  if has_column_privilege('anon', 'public.events', 'centroid', 'select')
     or has_column_privilege('anon', 'public.claim_groups', 'centroid', 'select')
     or has_column_privilege('anon', 'public.stories', 'centroid', 'select') then
    raise exception 'centroid остался доступен анониму';
  end if;

  if has_table_privilege('anon', 'public.gray_zone_verdicts', 'select')
     or has_table_privilege('anon', 'public.monitoring_checks', 'select') then
    raise exception 'служебные таблицы остались доступны анониму';
  end if;

  -- Фронт должен продолжать читать публичное.
  if not (has_column_privilege('anon', 'public.events', 'last_article_at', 'select')
          and has_table_privilege('anon', 'public.event_articles', 'select')
          and has_column_privilege('anon', 'public.articles', 'lead', 'select')) then
    raise exception 'анон потерял чтение публичного контента';
  end if;
end
$$;
