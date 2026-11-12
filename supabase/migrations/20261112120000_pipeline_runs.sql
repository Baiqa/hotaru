-- Фаза 2 плана P0 (tmp/plans/p0-ispravleniya-posle-audita.md): журнал запусков
-- шагов конвейера. Каждый шаг воркера (hotaru-collect, hotaru-cluster-events, ...)
-- пишет сюда строку при старте и закрывает её при завершении — см.
-- worker/src/hotaru_worker/runlog.py.
--
-- Зачем: (1) редкие шаги (разделение событий, ряды World Bank, мониторинг)
-- запускаются по этому журналу ("прошло ли N часов с последнего успеха"), а не по
-- отдельному cron; (2) видно, какой шаг сколько работал и чем закончился — раньше
-- это было только в логах GitHub Actions, которые без авторизации недоступны.
--
-- status='running' с пустым finished_at у старой строки = шаг был убит снаружи
-- (жёсткий таймаут GitHub), не успев закрыть запись.

create table pipeline_runs (
  id uuid primary key default gen_random_uuid(),
  step text not null,
  status text not null default 'running' check (status in ('running', 'ok', 'failed')),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  stats jsonb not null default '{}'::jsonb
);

create index pipeline_runs_step_started_at_idx on pipeline_runs (step, started_at desc);

-- Служебная таблица воркера (как gray_zone_verdicts и monitoring_checks): RLS без
-- политик, плюс явный отзыв грантов — чтобы доступ не держался на одном RLS.
alter table pipeline_runs enable row level security;
revoke all on pipeline_runs from anon, authenticated;
