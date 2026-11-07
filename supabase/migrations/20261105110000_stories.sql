-- Этап 4 (без LLM-части, раздел 3.3/5 CLAUDE.md): привязка события к сюжету
-- через мягкий косинус-порог + общую сущность (story_assignment.py), без
-- majority-entity overlap как у event_merge — сущности в сюжете естественно
-- дрейфуют между "решение -> реакция -> последствие".

create table stories (
  id uuid primary key default gen_random_uuid(),
  title text not null,       -- заглушка: title события-основателя, как events.title
  started_at timestamptz not null,
  updated_at timestamptz not null default now(),
  centroid vector(768),
  status text not null default 'active' check (status in ('active', 'merged')),
  created_at timestamptz not null default now()
);
-- 'merged' зарезервирован на будущее (по аналогии с events) — merge-джобы для
-- сюжетов в этом раунде нет, никто пока не пишет это значение.

create table story_events (
  event_id uuid primary key references events(id) on delete cascade,
  story_id uuid not null references stories(id) on delete cascade,
  position int not null,
  joined_at timestamptz not null default now()
);

create index story_events_story_id_idx on story_events (story_id);

alter table stories enable row level security;
alter table story_events enable row level security;

create policy "stories are publicly readable"
  on stories for select
  to anon, authenticated
  using (true);

create policy "story_events are publicly readable"
  on story_events for select
  to anon, authenticated
  using (true);
