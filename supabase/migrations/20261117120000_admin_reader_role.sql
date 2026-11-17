-- Роль admin_reader для страницы /admin (план tmp/plans/admin-dashboard.md).
--
-- Страница серверная и только читает. Ходит в БД не service_role (полный доступ на запись),
-- а этой ролью: только SELECT, только нужные колонки, без текстов статей/фактов/сводок и
-- без центроидов. Роль не может писать даже при утечке пароля.
--
-- Пароль здесь НЕ задаётся (файл в git). Роль создаётся без входа; логин и пароль
-- включаются отдельной командой вне репозитория:
--   alter role admin_reader with login password '...';
--
-- Служебные таблицы (pipeline_runs, gray_zone_verdicts) закрыты RLS без политик, поэтому
-- роли даны явные SELECT-политики. BYPASSRLS в Supabase выдать нельзя (нужен суперпользователь).

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'admin_reader') then
    create role admin_reader nologin;
  end if;
end
$$;

-- Защита уровня роли: любые транзакции только на чтение, короткий таймаут запроса,
-- не больше нескольких соединений.
alter role admin_reader set default_transaction_read_only = on;
alter role admin_reader set statement_timeout = '15s';
alter role admin_reader connection limit 5;

grant usage on schema public to admin_reader;

-- Колонки без текстов и векторов-центроидов. embedding нужен только для «is null»
-- (сколько статей/фактов ещё без эмбеддинга) — значение страница не выбирает.
grant select (id, source_id, title, published_at, created_at, fetch_status,
              entities_extracted_at, embedding, claims_extracted_at, claims_attempts)
  on articles to admin_reader;
grant select (id, name, base_url, fetch_method, rss_url, lang, robots_checked_at)
  on sources to admin_reader;
grant select (id, canonical_id, title, started_at, updated_at, status, created_at,
              last_article_at, source_count, rendered_source_count, render_attempts, render_failed_at)
  on events to admin_reader;
grant select on event_articles to admin_reader;
grant select (id, article_id, type, created_at, embedding) on claims to admin_reader;
grant select (id, event_id, bucket, source_count, created_at, updated_at) on claim_groups to admin_reader;
grant select (claim_id, claim_group_id, relation) on claim_group_members to admin_reader;
grant select (id, event_id, format, lang, model, created_at, updated_at) on renders to admin_reader;
grant select (id, title, started_at, updated_at, status, created_at) on stories to admin_reader;
grant select on story_events to admin_reader;
grant select on pipeline_runs to admin_reader;
grant select on gray_zone_verdicts to admin_reader;
grant select (id, type) on entities to admin_reader;
grant select on article_entities to admin_reader;

-- RLS включён на этих таблицах; без политики роль не увидела бы ни одной строки.
do $$
declare
  t text;
begin
  foreach t in array array[
    'articles', 'sources', 'events', 'event_articles', 'claims', 'claim_groups',
    'claim_group_members', 'renders', 'stories', 'story_events', 'pipeline_runs',
    'gray_zone_verdicts', 'entities', 'article_entities'
  ]
  loop
    if not exists (
      select 1 from pg_policies
      where schemaname = 'public' and tablename = t and policyname = 'admin_reader_select'
    ) then
      execute format('create policy admin_reader_select on public.%I for select to admin_reader using (true)', t);
    end if;
  end loop;
end
$$;
