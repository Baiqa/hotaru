-- Этап 2, подзадача "Извлечение фактов": claims + claim_numbers, с evidence как
-- обязательным жёстким барьером против галлюцинаций (раздел 3.2 CLAUDE.md).
-- embedding vector на claims (упомянут в разделе 4) — отдельной миграцией позже, когда
-- понадобится для "схлопывания фактов" (следующая подзадача Этапа 2), по тому же
-- принципу, что articles.embedding добавляли отдельно от исходной таблицы articles.

create table claims (
  id uuid primary key default gen_random_uuid(),
  article_id uuid not null references articles(id) on delete cascade,
  text text not null,
  type text not null check (type in ('fact', 'statement', 'estimate')),
  evidence text not null,
  created_at timestamptz not null default now()
);

create index claims_article_id_idx on claims (article_id);

create table claim_numbers (
  id uuid primary key default gen_random_uuid(),
  claim_id uuid not null references claims(id) on delete cascade,
  value numeric not null,
  unit text,
  label text,
  period text
);

create index claim_numbers_claim_id_idx on claim_numbers (claim_id);

-- Кеш по хешу текста статьи (раздел 3.2 CLAUDE.md) — content_hash уже unique на
-- articles, так что "эта статья уже обработана" эквивалентно "у этой строки проставлен
-- claims_extracted_at", по образцу entities_extracted_at из Этапа 1.
alter table articles add column claims_extracted_at timestamptz;

alter table claims enable row level security;
alter table claim_numbers enable row level security;

create policy "claims are publicly readable"
  on claims for select
  to anon, authenticated
  using (true);

create policy "claim_numbers are publicly readable"
  on claim_numbers for select
  to anon, authenticated
  using (true);
