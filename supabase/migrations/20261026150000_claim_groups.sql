-- Этап 2, подзадача "Схлопывание фактов, три корзины" (раздел 3.4/5 CLAUDE.md).
-- Генерация текста (сводка/разбор), пост-проверка, renders/render_claims — следующие
-- подзадачи.

-- embedding на claims — упомянут в разделе 4 CLAUDE.md, отложен в миграции claims до
-- реального потребителя (этот самый шаг: сопоставление claims между статьями события).
alter table claims add column embedding vector(768);
create index claims_embedding_hnsw_idx on claims using hnsw (embedding vector_cosine_ops);

create table claim_groups (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references events(id) on delete cascade,
  bucket text not null check (bucket in ('confirmed', 'unique', 'disputed')),
  source_count int not null default 0,
  centroid vector(768),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index claim_groups_event_id_idx on claim_groups (event_id);

create trigger claim_groups_set_updated_at
  before update on claim_groups
  for each row
  execute function set_updated_at();

-- claim_id — сам PK: claim принадлежит ровно одной группе (внутри своего события).
-- relation='contradicts' — этот claim РАСХОДИТСЯ с другими участниками группы (а не
-- просто подтверждает их) — по этому полю считается bucket='disputed'.
create table claim_group_members (
  claim_id uuid primary key references claims(id) on delete cascade,
  claim_group_id uuid not null references claim_groups(id) on delete cascade,
  relation text not null default 'confirms' check (relation in ('confirms', 'contradicts')),
  similarity_score double precision,
  joined_at timestamptz not null default now()
);

create index claim_group_members_group_id_idx on claim_group_members (claim_group_id);

alter table claim_groups enable row level security;
alter table claim_group_members enable row level security;

create policy "claim_groups are publicly readable"
  on claim_groups for select
  to anon, authenticated
  using (true);

create policy "claim_group_members are publicly readable"
  on claim_group_members for select
  to anon, authenticated
  using (true);
