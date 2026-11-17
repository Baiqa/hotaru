import "server-only";
import { requireAdmin } from "./admin-auth";
import { query } from "./admin-db";

// Данные страницы /admin. Всё считается в SQL; числа приводятся к ::int (иначе pg отдаёт
// bigint строками). Время хранится и показывается в UTC.

export type ArticleFunnel = {
  total: number;
  ok: number;
  partial: number;
  entities: number;
  embedded: number;
  inEvent: number;
  claimsDone: number;
  okWithoutClaims: number;
};

export type ClaimFunnel = { total: number; embedded: number; grouped: number };

export type EventFunnel = { active: number; merged: number; multiSource: number; multiRendered: number };

export type StepLastRun = {
  step: string;
  status: "running" | "ok" | "failed";
  startedAt: string;
  finishedAt: string | null;
  stats: Record<string, unknown>;
  lastOkAt: string | null;
};

export type StepAggregate = {
  step: string;
  runs: number;
  failed: number;
  stale: number;
  processed: number;
  grayRequests: number;
  skippedLlm: number;
};

export type RecentRun = {
  id: string;
  step: string;
  status: "running" | "ok" | "failed";
  startedAt: string;
  finishedAt: string | null;
  stats: Record<string, unknown>;
};

export type SizeBucket = { bucket: string; events: number; articles: number };

export type ClusterRun = {
  startedAt: string;
  processed: number;
  // null — запуск не записывал это поле (старые запуски): нет данных, а не ноль.
  grayRequests: number | null;
  joined: number;
  newEvents: number;
};

export type Verdict = { verdict: string; count: number };

export type TopEvent = { id: string; title: string; articles: number; sources: number; spanHours: number | null };

export type SourceRow = {
  name: string;
  total: number;
  last24h: number;
  partial: number;
  lastAt: string | null;
};

export type DailyPoint = { day: string; source: string; count: number };

export type AdminStats = {
  generatedAt: string;
  articles: ArticleFunnel;
  claims: ClaimFunnel;
  events: EventFunnel;
  stepsLast: StepLastRun[];
  stepsToday: StepAggregate[];
  steps24h: StepAggregate[];
  recentRuns: RecentRun[];
  lastAnyRunAt: string | null;
  sizeDistribution: SizeBucket[];
  clusterRuns: ClusterRun[];
  verdicts7d: Verdict[];
  topEvents: TopEvent[];
  sources: SourceRow[];
  daily: DailyPoint[];
};

type Iso = string | null;
const iso = (value: Date | string | null): Iso => (value === null ? null : new Date(value).toISOString());

function stepAggregates(rows: Array<Record<string, number | string>>): StepAggregate[] {
  return rows.map((r) => ({
    step: String(r.step),
    runs: Number(r.runs),
    failed: Number(r.failed),
    stale: Number(r.stale),
    processed: Number(r.processed),
    grayRequests: Number(r.gray_req),
    skippedLlm: Number(r.skipped),
  }));
}

// Агрегат по журналу pipeline_runs с начала окна. Значения stats — json, поэтому
// приведение с защитой от нечислового содержимого.
const STEP_AGG_SQL = (since: string) => `
  select step,
         count(*)::int runs,
         (count(*) filter (where status = 'failed'))::int failed,
         (count(*) filter (where status = 'running' and started_at < now() - interval '3 hours'))::int stale,
         coalesce(sum(case when stats->>'processed' ~ '^[0-9]+$' then (stats->>'processed')::int end), 0)::int processed,
         coalesce(sum(case when stats->>'gray_zone_requests' ~ '^[0-9]+$' then (stats->>'gray_zone_requests')::int end), 0)::int gray_req,
         coalesce(sum(case when stats->>'skipped_llm_unavailable' ~ '^[0-9]+$' then (stats->>'skipped_llm_unavailable')::int end), 0)::int skipped
  from pipeline_runs
  where started_at >= ${since}
  group by step
  order by step`;

async function loadStats(): Promise<AdminStats> {
  const [
    articles,
    claims,
    events,
    stepsLast,
    stepsToday,
    steps24h,
    recentRuns,
    lastAny,
    sizes,
    clusterRuns,
    verdicts,
    topEvents,
    sources,
    daily,
  ] = await Promise.all([
    query<Record<string, number>>(`
      select count(*)::int total,
             (count(*) filter (where fetch_status = 'ok'))::int ok,
             (count(*) filter (where fetch_status = 'partial'))::int partial,
             (count(*) filter (where entities_extracted_at is not null))::int entities,
             (count(*) filter (where embedding is not null))::int embedded,
             (count(*) filter (where exists (select 1 from event_articles ea where ea.article_id = a.id)))::int in_event,
             (count(*) filter (where claims_extracted_at is not null))::int claims_done,
             (count(*) filter (where fetch_status = 'ok' and claims_extracted_at is null))::int ok_without_claims
      from articles a`),
    query<Record<string, number>>(`
      select count(*)::int total,
             (count(*) filter (where embedding is not null))::int embedded,
             (count(*) filter (where exists (select 1 from claim_group_members m where m.claim_id = c.id)))::int grouped
      from claims c`),
    query<Record<string, number>>(`
      select (count(*) filter (where status = 'active'))::int active,
             (count(*) filter (where status = 'merged'))::int merged,
             (count(*) filter (where status = 'active' and source_count >= 2))::int multi,
             (count(*) filter (where status = 'active' and source_count >= 2
                               and exists (select 1 from renders r where r.event_id = e.id)))::int multi_rendered
      from events e`),
    query<Record<string, unknown>>(`
      select distinct on (step) step, status, started_at, finished_at, stats,
             (select max(p2.finished_at) from pipeline_runs p2 where p2.step = p.step and p2.status = 'ok') last_ok
      from pipeline_runs p
      order by step, started_at desc`),
    query<Record<string, number | string>>(STEP_AGG_SQL(`date_trunc('day', now() at time zone 'utc') at time zone 'utc'`)),
    query<Record<string, number | string>>(STEP_AGG_SQL(`now() - interval '24 hours'`)),
    query<Record<string, unknown>>(`
      select id, step, status, started_at, finished_at, stats
      from pipeline_runs order by started_at desc limit 15`),
    query<{ at: Date | null }>(`select max(started_at) as at from pipeline_runs`),
    query<Record<string, number | string>>(`
      with sz as (
        select ea.event_id, count(*)::int n
        from event_articles ea join events e on e.id = ea.event_id
        where e.status = 'active' group by 1
      )
      select case when n = 1 then '1' when n <= 4 then '2–4' when n <= 9 then '5–9'
                  when n <= 19 then '10–19' when n <= 39 then '20–39' else '40+' end bucket,
             count(*)::int events, sum(n)::int articles, min(n) lo
      from sz group by 1 order by min(n)`),
    query<Record<string, unknown>>(`
      select started_at,
             case when stats->>'processed' ~ '^[0-9]+$' then (stats->>'processed')::int else 0 end processed,
             -- у запусков до 15.11 поля нет: это «нет данных», а не ноль запросов
             case when stats->>'gray_zone_requests' ~ '^[0-9]+$' then (stats->>'gray_zone_requests')::int end gray_req,
             case when stats->>'joined' ~ '^[0-9]+$' then (stats->>'joined')::int else 0 end joined,
             case when stats->>'new_events' ~ '^[0-9]+$' then (stats->>'new_events')::int else 0 end new_events
      from pipeline_runs
      where step = 'cluster-events' and status = 'ok'
      order by started_at desc limit 10`),
    query<{ verdict: string; count: number }>(`
      select verdict, count(*)::int as count from gray_zone_verdicts
      where created_at > now() - interval '7 days' group by 1 order by 2 desc`),
    query<Record<string, unknown>>(`
      select e.id, e.title, count(*)::int n, count(distinct a.source_id)::int sources,
             round(extract(epoch from max(a.published_at) - min(a.published_at)) / 3600)::int span_h
      from events e
      join event_articles ea on ea.event_id = e.id
      join articles a on a.id = ea.article_id
      where e.status = 'active'
      group by e.id order by n desc limit 10`),
    query<Record<string, unknown>>(`
      select s.name,
             count(a.id)::int total,
             (count(a.id) filter (where a.created_at > now() - interval '24 hours'))::int last24h,
             (count(a.id) filter (where a.fetch_status = 'partial'))::int partial,
             max(a.created_at) last_at
      from sources s left join articles a on a.source_id = s.id
      group by s.id, s.name order by total desc`),
    query<{ day: string; source: string; count: number }>(`
      select to_char(a.created_at at time zone 'utc', 'YYYY-MM-DD') as day, s.name as source, count(*)::int as count
      from articles a join sources s on s.id = a.source_id
      where a.created_at > now() - interval '14 days'
      group by 1, 2 order by 1`),
  ]);

  const a = articles[0];
  const c = claims[0];
  const e = events[0];

  return {
    generatedAt: new Date().toISOString(),
    articles: {
      total: a.total,
      ok: a.ok,
      partial: a.partial,
      entities: a.entities,
      embedded: a.embedded,
      inEvent: a.in_event,
      claimsDone: a.claims_done,
      okWithoutClaims: a.ok_without_claims,
    },
    claims: { total: c.total, embedded: c.embedded, grouped: c.grouped },
    events: { active: e.active, merged: e.merged, multiSource: e.multi, multiRendered: e.multi_rendered },
    stepsLast: stepsLast.map((r) => ({
      step: String(r.step),
      status: r.status as StepLastRun["status"],
      startedAt: iso(r.started_at as Date)!,
      finishedAt: iso(r.finished_at as Date | null),
      stats: (r.stats ?? {}) as Record<string, unknown>,
      lastOkAt: iso(r.last_ok as Date | null),
    })),
    stepsToday: stepAggregates(stepsToday),
    steps24h: stepAggregates(steps24h),
    recentRuns: recentRuns.map((r) => ({
      id: String(r.id),
      step: String(r.step),
      status: r.status as RecentRun["status"],
      startedAt: iso(r.started_at as Date)!,
      finishedAt: iso(r.finished_at as Date | null),
      stats: (r.stats ?? {}) as Record<string, unknown>,
    })),
    lastAnyRunAt: iso(lastAny[0]?.at ?? null),
    sizeDistribution: sizes.map((r) => ({
      bucket: String(r.bucket),
      events: Number(r.events),
      articles: Number(r.articles),
    })),
    clusterRuns: clusterRuns.map((r) => ({
      startedAt: iso(r.started_at as Date)!,
      processed: Number(r.processed),
      grayRequests: r.gray_req === null ? null : Number(r.gray_req),
      joined: Number(r.joined),
      newEvents: Number(r.new_events),
    })),
    verdicts7d: verdicts,
    topEvents: topEvents.map((r) => ({
      id: String(r.id),
      title: String(r.title),
      articles: Number(r.n),
      sources: Number(r.sources),
      spanHours: r.span_h === null ? null : Number(r.span_h),
    })),
    sources: sources.map((r) => ({
      name: String(r.name),
      total: Number(r.total),
      last24h: Number(r.last24h),
      partial: Number(r.partial),
      lastAt: iso(r.last_at as Date | null),
    })),
    daily,
  };
}

// Небольшой кеш на процесс: частые обновления страницы не должны гонять тяжёлые запросы.
const CACHE_MS = 30_000;
let cache: { at: number; promise: Promise<AdminStats> } | null = null;

export async function getAdminStats(): Promise<AdminStats> {
  await requireAdmin();
  if (cache && Date.now() - cache.at < CACHE_MS) return cache.promise;
  const promise = loadStats();
  cache = { at: Date.now(), promise };
  // Неудачный результат в кеше не держим.
  promise.catch(() => {
    if (cache?.promise === promise) cache = null;
  });
  return promise;
}
