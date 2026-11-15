-- Фаза 5 плана P0 (tmp/plans/p0-ispravleniya-posle-audita.md): бюджет LLM.
--
-- 5.1. Денормализованные поля события. Очередь синтеза должна выбирать события по
-- «сколько изданий» и «когда пришла последняя статья». Считать это на лету в
-- запросе очереди (join event_articles -> articles по ~4000 событий) дорого, поэтому
-- числа хранятся в events и пересчитываются триггером на event_articles. Триггер, а не
-- вызовы из кода: статьи попадают в события и покидают их в четырёх разных местах
-- воркера (присоединение, создание, слияние, разделение), и любое новое место
-- получит пересчёт автоматически.
--
-- 5.2. Учёт неудач синтеза: render_attempts / render_failed_at (после трёх неудач
-- событие откладывается на сутки) и rendered_source_count — сколько изданий было у
-- события, когда писали сводку (если стало больше, сводку пересоздаём).
--
-- 5.3. articles.claims_attempts — счётчик неудачных извлечений фактов, чтобы
-- «отравленная» статья не блокировала очередь.

alter table events
  add column last_article_at timestamptz,
  add column source_count integer not null default 0,
  add column rendered_source_count integer,
  add column render_attempts integer not null default 0,
  add column render_failed_at timestamptz;

alter table articles
  add column claims_attempts integer not null default 0;

create or replace function refresh_event_stats(p_event_id uuid) returns void
language sql
set search_path = public
as $$
  update events e
  set last_article_at = s.last_at,
      source_count = coalesce(s.sources, 0)
  from (
    select max(a.published_at) as last_at, count(distinct a.source_id) as sources
    from event_articles ea
    join articles a on a.id = ea.article_id
    where ea.event_id = p_event_id
  ) s
  where e.id = p_event_id;
$$;

create or replace function event_articles_refresh_stats() returns trigger
language plpgsql
set search_path = public
as $$
begin
  if tg_op in ('INSERT', 'UPDATE') then
    perform refresh_event_stats(new.event_id);
  end if;
  if tg_op = 'DELETE' or (tg_op = 'UPDATE' and old.event_id is distinct from new.event_id) then
    perform refresh_event_stats(old.event_id);
  end if;
  return null;
end;
$$;

create trigger event_articles_refresh_stats
  after insert or update of event_id or delete on event_articles
  for each row execute function event_articles_refresh_stats();

-- Служебные функции воркера: не должны вызываться через публичный API (/rpc).
revoke execute on function refresh_event_stats(uuid) from public, anon, authenticated;
revoke execute on function event_articles_refresh_stats() from public, anon, authenticated;

-- Начальные значения для уже существующих событий.
select refresh_event_stats(id) from events;

-- У событий, для которых сводка уже есть, считаем её актуальной на сегодня:
-- иначе первый же запуск пересоздал бы все 127 сводок «потому что изданий стало больше».
update events e
set rendered_source_count = e.source_count
where exists (select 1 from renders r where r.event_id = e.id and r.format = 'summary_30s');

-- Очередь синтеза выбирает активные свежие события по этому индексу.
create index events_render_queue_idx on events (last_article_at desc)
  where status = 'active';
