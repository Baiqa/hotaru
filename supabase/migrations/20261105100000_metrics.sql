-- Этап 3 MVP (раздел 3.5/4 CLAUDE.md): только World Bank, только линия, только
-- allowlist по правилу (не LLM-классификатор) — см. worker/src/hotaru_worker/metrics.py
-- и web/src/lib/metrics.ts.

create table metric_series (
  id uuid primary key default gen_random_uuid(),
  key text not null unique,        -- 'FP.CPI.TOTL.ZG:KAZ' (indicator:country)
  label text not null,             -- "Инфляция, % в год — Казахстан"
  unit text not null,              -- "%"
  source_name text not null,       -- "World Bank"
  source_url text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger metric_series_set_updated_at
  before update on metric_series
  for each row
  execute function set_updated_at();

create table metric_points (
  id uuid primary key default gen_random_uuid(),
  series_id uuid not null references metric_series(id) on delete cascade,
  period text not null,            -- год как строка, тот же тип, что claim_numbers.period
  value numeric not null,
  unique (series_id, period)
);

create index metric_points_series_id_idx on metric_points (series_id);

alter table metric_series enable row level security;
alter table metric_points enable row level security;

create policy "metric_series are publicly readable"
  on metric_series for select
  to anon, authenticated
  using (true);

create policy "metric_points are publicly readable"
  on metric_points for select
  to anon, authenticated
  using (true);
