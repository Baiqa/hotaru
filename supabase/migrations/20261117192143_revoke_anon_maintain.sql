-- Дополнение к 20261117191848_restrict_anon_writes_and_centroids.sql.
--
-- В Postgres 17 у таблиц есть отдельный грант MAINTAIN (VACUUM, ANALYZE, REFRESH, CLUSTER,
-- REINDEX, LOCK TABLE). Supabase по умолчанию выдаёт его anon и authenticated вместе с остальными,
-- а предыдущая миграция его не перечисляла (проверка после применения показала 17 таблиц).
-- Фронту он не нужен: через REST такие операции недоступны, но грант лишний.

revoke maintain on all tables in schema public from anon, authenticated;

alter default privileges for role postgres in schema public
  revoke maintain on tables from anon, authenticated;

do $$
declare
  bad text;
begin
  select string_agg(c.relname || ':' || r, ', ') into bad
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  cross join unnest(array['anon', 'authenticated']) as r
  where n.nspname = 'public' and c.relkind = 'r'
    and has_table_privilege(r, c.oid, 'MAINTAIN');
  if bad is not null then
    raise exception 'у anon/authenticated остался MAINTAIN: %', bad;
  end if;
end
$$;
