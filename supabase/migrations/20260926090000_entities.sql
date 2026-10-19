-- Этап 1, подзадача "Сущности": извлечение, таблица алиасов, нормализация (раздел 5 CLAUDE.md).
-- embedding vector / pgvector-индекс — следующая подзадача Этапа 1, не эта миграция.

create table entities (
  id uuid primary key default gen_random_uuid(),
  canonical_name text not null,
  type text not null check (type in ('person', 'org', 'place')),
  external_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (canonical_name, type)
);

create trigger entities_set_updated_at
  before update on entities
  for each row
  execute function set_updated_at();

-- Один алиас (в данном языке) резолвится ровно в одну сущность — так резолюция
-- в воркере остаётся детерминированной. Омонимия разных сущностей с одинаковым
-- именем — известное упрощение этой подзадачи (раздел 5 CLAUDE.md относит такие
-- случаи, как компании-тёзки, к кластеризации событий, не к таблице сущностей).
create table entity_aliases (
  id uuid primary key default gen_random_uuid(),
  entity_id uuid not null references entities(id) on delete cascade,
  alias text not null,
  lang text not null check (lang in ('ru', 'kk', 'en')),
  created_at timestamptz not null default now()
);

create unique index entity_aliases_alias_lang_key on entity_aliases (lower(alias), lang);
create index entity_aliases_entity_id_idx on entity_aliases (entity_id);

create table article_entities (
  article_id uuid not null references articles(id) on delete cascade,
  entity_id uuid not null references entities(id) on delete cascade,
  mention_count integer not null default 1,
  primary key (article_id, entity_id)
);

create index article_entities_entity_id_idx on article_entities (entity_id);

-- Отмечает статьи, для которых уже прогнали извлечение сущностей — по образцу
-- sources.robots_checked_at, чтобы не гонять NER по уже обработанным статьям
-- на каждом прогоне hotaru-extract-entities.
alter table articles add column entities_extracted_at timestamptz;

alter table entities enable row level security;
alter table entity_aliases enable row level security;
alter table article_entities enable row level security;

create policy "entities are publicly readable"
  on entities for select
  to anon, authenticated
  using (true);

create policy "entity_aliases are publicly readable"
  on entity_aliases for select
  to anon, authenticated
  using (true);

create policy "article_entities are publicly readable"
  on article_entities for select
  to anon, authenticated
  using (true);
