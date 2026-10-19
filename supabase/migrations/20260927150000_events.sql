-- Этап 1, подзадача "Два порога + LLM серой зоны + events/event_articles" (раздел 5 CLAUDE.md).
-- Фоновые merge/split задачи, сюжеты/event_links, синтез текста — следующие подзадачи.

create table events (
  id uuid primary key default gen_random_uuid(),
  -- Задел под будущий merge (раздел 3.3: "у события должен быть canonical_id — старые
  -- ссылки не должны умирать"). null = событие само каноническое.
  canonical_id uuid references events(id),
  -- Пока = заголовок статьи-основателя события; полноценный синтез текста — Этап 2.
  title text not null,
  started_at timestamptz not null,
  updated_at timestamptz not null default now(),
  centroid vector(768),
  status text not null default 'active' check (status in ('active', 'merged')),
  created_at timestamptz not null default now()
);

create trigger events_set_updated_at
  before update on events
  for each row
  execute function set_updated_at();

-- article_id — сам PK: статья принадлежит ровно одному событию.
create table event_articles (
  article_id uuid primary key references articles(id) on delete cascade,
  event_id uuid not null references events(id) on delete cascade,
  -- null для статьи-основателя события (сравнивать было не с чем).
  similarity_score double precision,
  joined_at timestamptz not null default now()
);

create index event_articles_event_id_idx on event_articles (event_id);

-- Раздел 3.3: "Ответы [LLM] кешируются по паре id" — этой таблицы нет в разделе 4
-- CLAUDE.md, добавлена под явное требование кеша. Пара хранится в порядке
-- article_a_id < article_b_id (нормализация на уровне приложения).
create table gray_zone_verdicts (
  article_a_id uuid not null references articles(id) on delete cascade,
  article_b_id uuid not null references articles(id) on delete cascade,
  verdict text not null check (verdict in ('same', 'related', 'different')),
  created_at timestamptz not null default now(),
  primary key (article_a_id, article_b_id)
);

alter table events enable row level security;
alter table event_articles enable row level security;
alter table gray_zone_verdicts enable row level security;

create policy "events are publicly readable"
  on events for select
  to anon, authenticated
  using (true);

create policy "event_articles are publicly readable"
  on event_articles for select
  to anon, authenticated
  using (true);

-- gray_zone_verdicts — служебный кеш воркера, не нужен фронту.
