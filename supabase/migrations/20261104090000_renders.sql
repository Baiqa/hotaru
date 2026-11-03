-- Этап 2, подзадача "Синтез текста" (раздел 3.4/5 CLAUDE.md).
-- Модель-синтезатор не видит статьи, только факты события (claims + их bucket из
-- claim_groups). render_claims — то, что делает возможной проверку "каждое
-- предложение имеет источник" (готовность Этапа 2).

create table renders (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references events(id) on delete cascade,
  format text not null check (format in ('summary_30s', 'full_breakdown')),
  lang text not null default 'ru',
  body text not null,
  model text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (event_id, format, lang)
);

create index renders_event_id_idx on renders (event_id);

create trigger renders_set_updated_at
  before update on renders
  for each row
  execute function set_updated_at();

-- Предложение может ссылаться на несколько claim_id (напр. сравнение двух чисел) —
-- поэтому claim_id часть первичного ключа, а не отдельная строка на предложение.
create table render_claims (
  render_id uuid not null references renders(id) on delete cascade,
  sentence_index int not null,
  claim_id uuid not null references claims(id) on delete cascade,
  primary key (render_id, sentence_index, claim_id)
);

alter table renders enable row level security;
alter table render_claims enable row level security;

create policy "renders are publicly readable"
  on renders for select
  to anon, authenticated
  using (true);

create policy "render_claims are publicly readable"
  on render_claims for select
  to anon, authenticated
  using (true);
