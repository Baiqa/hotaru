-- Этап 0: sources + articles (раздел 5 CLAUDE.md)
-- embedding vector и pgvector-индекс добавляются отдельной миграцией в Этапе 1.

create or replace function set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create table sources (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  base_url text not null unique,
  fetch_method text not null check (fetch_method in ('rss', 'httpx_trafilatura', 'curl_cffi', 'patchright')),
  rss_url text,
  lang text not null check (lang in ('ru', 'kk', 'en')),
  robots_checked_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger sources_set_updated_at
  before update on sources
  for each row
  execute function set_updated_at();

create table articles (
  id uuid primary key default gen_random_uuid(),
  source_id uuid not null references sources(id) on delete restrict,
  url text not null,
  canonical_url text not null unique,
  title text not null,
  lead text,
  body text,
  published_at timestamptz,
  content_hash text not null unique,
  fetch_status text not null default 'pending' check (fetch_status in ('pending', 'ok', 'partial', 'failed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index articles_source_id_published_at_idx on articles (source_id, published_at desc);
create index articles_fetch_status_idx on articles (fetch_status);

create trigger articles_set_updated_at
  before update on articles
  for each row
  execute function set_updated_at();

-- Публичное чтение (новости открытые); запись — только service_role (воркер), RLS её не ограничивает.
alter table sources enable row level security;
alter table articles enable row level security;

create policy "sources are publicly readable"
  on sources for select
  to anon, authenticated
  using (true);

create policy "articles are publicly readable"
  on articles for select
  to anon, authenticated
  using (true);
