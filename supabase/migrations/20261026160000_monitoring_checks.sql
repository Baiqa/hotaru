-- Раздел 6 CLAUDE.md: "проверки, которые должны работать постоянно". Таблица —
-- исторический, инспектируемый след прогонов (не просто лог) по каждой из четырёх
-- проверок раздела 6.
create table monitoring_checks (
  id uuid primary key default gen_random_uuid(),
  check_name text not null check (check_name in (
    'source_silent_24h', 'text_length_drop', 'render_rejection_rate', 'gray_zone_share'
  )),
  status text not null check (status in ('ok', 'alert', 'no_data')),
  details jsonb not null default '{}'::jsonb,
  checked_at timestamptz not null default now()
);

create index monitoring_checks_name_checked_at_idx on monitoring_checks (check_name, checked_at desc);

alter table monitoring_checks enable row level security;
-- Служебная таблица воркера (как gray_zone_verdicts) — не контент для фронта,
-- публичной select-политики нет.
