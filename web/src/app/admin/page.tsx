import Link from "next/link";
import { requireAdmin } from "@/lib/admin-auth";
import { getAdminStats, type AdminStats } from "@/lib/admin-stats";
import {
  BarList,
  Card,
  StackedDaily,
  StatusPill,
  fmtAgo,
  fmtDuration,
  fmtTime,
  num,
  pct,
} from "@/components/admin/AdminCharts";
import { logout } from "./login/actions";

export const dynamic = "force-dynamic";

// Пока этот порог не пройден, считаем конвейер «живым»; дальше показываем предупреждение.
const STALE_PIPELINE_HOURS = 3;

function Th({ children, right = false }: { children: React.ReactNode; right?: boolean }) {
  return (
    <th className={`whitespace-nowrap px-2 py-1.5 text-xs font-medium text-zinc-500 ${right ? "text-right" : "text-left"}`}>
      {children}
    </th>
  );
}

function Td({ children, right = false, mono = false }: { children: React.ReactNode; right?: boolean; mono?: boolean }) {
  return (
    <td
      className={`px-2 py-1.5 align-top text-sm text-zinc-800 dark:text-zinc-200 ${right ? "text-right tabular-nums" : ""} ${mono ? "font-mono text-xs" : ""}`}
    >
      {children}
    </td>
  );
}

function Table({ children }: { children: React.ReactNode }) {
  // Широкая таблица прокручивается внутри карточки, а не расширяет страницу.
  return (
    <div className="-mx-1 overflow-x-auto">
      <table className="w-full border-collapse">{children}</table>
    </div>
  );
}

function Funnels({ stats }: { stats: AdminStats }) {
  const { articles: a, claims: c, events: e } = stats;
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <Card title="Статьи" note="Доля от всех собранных. «Ждут» — сколько не дошло до шага.">
        <BarList
          rows={[
            { label: "Собрано", value: a.total, of: a.total },
            { label: "С полным текстом", value: a.ok, of: a.total, hint: `без текста (partial): ${num(a.partial)}` },
            { label: "Сущности извлечены", value: a.entities, of: a.total, waiting: a.total - a.entities },
            { label: "Эмбеддинг есть", value: a.embedded, of: a.total, waiting: a.total - a.embedded },
            { label: "Входят в событие", value: a.inEvent, of: a.total, waiting: a.total - a.inEvent },
            {
              label: "Факты извлечены",
              value: a.claimsDone,
              of: a.total,
              waiting: a.okWithoutClaims,
              hint: "ждут — статьи с текстом без фактов",
            },
          ]}
        />
      </Card>
      <Card title="Факты" note="Доля от всех извлечённых фактов.">
        <BarList
          tone="bg-violet-500"
          rows={[
            { label: "Извлечено фактов", value: c.total, of: c.total },
            { label: "С эмбеддингом", value: c.embedded, of: c.total, waiting: c.total - c.embedded },
            { label: "Сгруппировано", value: c.grouped, of: c.total, waiting: c.total - c.grouped },
          ]}
        />
      </Card>
      <Card title="События" note={`Слитых в другие (merged): ${num(e.merged)}.`}>
        <BarList
          tone="bg-emerald-500"
          rows={[
            { label: "Активных событий", value: e.active, of: e.active },
            { label: "С 2+ изданиями", value: e.multiSource, of: e.active },
            {
              label: "С 2+ изданиями и сводкой",
              value: e.multiRendered,
              of: e.multiSource,
              waiting: e.multiSource - e.multiRendered,
            },
          ]}
        />
      </Card>
    </div>
  );
}

function Steps({ stats }: { stats: AdminStats }) {
  const nowMs = new Date(stats.generatedAt).getTime();
  const today = new Map(stats.stepsToday.map((s) => [s.step, s]));
  const day = new Map(stats.steps24h.map((s) => [s.step, s]));
  const grayToday = stats.stepsToday.reduce((s, r) => s + r.grayRequests, 0);
  const skippedToday = stats.stepsToday.reduce((s, r) => s + r.skippedLlm, 0);

  return (
    <div className="space-y-4">
      <Card title="Шаги конвейера" note="Последний запуск каждого шага и счётчики. Время — UTC.">
        <Table>
          <thead>
            <tr className="border-b border-zinc-200 dark:border-zinc-800">
              <Th>Шаг</Th>
              <Th>Статус</Th>
              <Th>Последний запуск</Th>
              <Th right>Длит.</Th>
              <Th>Последний успех</Th>
              <Th right>За 24 ч: запусков / сбоев</Th>
              <Th right>Сегодня: обработано</Th>
            </tr>
          </thead>
          <tbody>
            {stats.stepsLast.map((s) => {
              const d = day.get(s.step);
              const t = today.get(s.step);
              const stale = s.status === "running" && nowMs - new Date(s.startedAt).getTime() > 3 * 3600_000;
              return (
                <tr key={s.step} className="border-b border-zinc-100 dark:border-zinc-900">
                  <Td mono>{s.step}</Td>
                  <Td>
                    <StatusPill status={s.status} stale={stale} />
                  </Td>
                  <Td>{fmtTime(s.startedAt)}</Td>
                  <Td right>{fmtDuration(s.startedAt, s.finishedAt)}</Td>
                  <Td>{fmtAgo(s.lastOkAt, nowMs)}</Td>
                  <Td right>
                    {d ? `${d.runs} / ${d.failed}` : "0 / 0"}
                    {d && d.stale > 0 ? <span className="text-red-600 dark:text-red-400"> · зависших {d.stale}</span> : null}
                  </Td>
                  <Td right>{t ? num(t.processed) : "0"}</Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="Нагрузка на LLM сегодня (оценка)"
          note="Остаток квот Gemini/Groq в БД не хранится: ниже то, что видно по журналу запусков."
        >
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <dt className="text-xs text-zinc-500">Запросов серой зоны</dt>
              <dd className="text-lg font-semibold tabular-nums text-zinc-900 dark:text-zinc-50">{num(grayToday)}</dd>
            </div>
            <div>
              <dt className="text-xs text-zinc-500">Пропущено из-за недоступной LLM</dt>
              <dd className="text-lg font-semibold tabular-nums text-zinc-900 dark:text-zinc-50">{num(skippedToday)}</dd>
            </div>
          </dl>
          <p className="mt-3 text-xs text-zinc-500">
            Ненулевое «пропущено» означает, что квота кончалась или модель отвечала ошибкой. Запросы фактов и синтеза в
            журнале не считаются отдельно, поэтому это нижняя граница.
          </p>
        </Card>

        <Card title="Последние запуски" note="15 последних записей журнала.">
          <ul className="space-y-1.5 text-sm">
            {stats.recentRuns.map((r) => (
              <li key={r.id} className="flex items-center justify-between gap-2">
                <span className="flex min-w-0 items-center gap-2">
                  <StatusPill status={r.status} />
                  <span className="truncate font-mono text-xs text-zinc-700 dark:text-zinc-300">{r.step}</span>
                </span>
                <span className="shrink-0 text-xs text-zinc-500">
                  {fmtTime(r.startedAt)} · {fmtDuration(r.startedAt, r.finishedAt)}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      </div>
    </div>
  );
}

function Quality({ stats }: { stats: AdminStats }) {
  const sizes = stats.sizeDistribution;
  const totalEvents = sizes.reduce((s, b) => s + b.events, 0);
  const totalArticles = sizes.reduce((s, b) => s + b.articles, 0);
  const singles = sizes.find((b) => b.bucket === "1");
  const runs = stats.clusterRuns;
  // В итог идут только запуски, где число запросов серой зоны записано.
  const measured = runs.filter((r) => r.grayRequests !== null);
  const runsProcessed = measured.reduce((s, r) => s + r.processed, 0);
  const runsGray = measured.reduce((s, r) => s + (r.grayRequests ?? 0), 0);
  const verdictTotal = stats.verdicts7d.reduce((s, v) => s + v.count, 0);
  const verdictLabel: Record<string, string> = { same: "то же событие", related: "связаны", different: "разные" };

  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="Размеры событий"
          note={`Событий: ${num(totalEvents)}, одиночных: ${singles ? pct(singles.events, totalEvents) : "—"}. Полоса — доля статей.`}
        >
          <BarList
            tone="bg-amber-500"
            rows={sizes.map((b) => ({
              label: `${b.bucket} ст.`,
              value: b.articles,
              of: totalArticles,
              hint: `событий: ${num(b.events)}`,
            }))}
          />
        </Card>

        <Card
          title="Серая зона по запускам кластеризации"
          note="Запросов к LLM на обработанную статью; до 2 запросов на статью, поэтому это верхняя оценка. Порог из спецификации — 25%."
        >
          <p className="mb-2 text-sm text-zinc-700 dark:text-zinc-300">
            За последние запуски с данными ({measured.length} из {runs.length}):{" "}
            <span className="font-semibold tabular-nums">{pct(runsGray, runsProcessed)}</span>{" "}
            <span className="text-xs text-zinc-500">
              ({num(runsGray)} запросов / {num(runsProcessed)} статей)
            </span>
          </p>
          <Table>
            <thead>
              <tr className="border-b border-zinc-200 dark:border-zinc-800">
                <Th>Запуск</Th>
                <Th right>Статей</Th>
                <Th right>Серая зона</Th>
                <Th right>Присоед.</Th>
                <Th right>Новых</Th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.startedAt} className="border-b border-zinc-100 dark:border-zinc-900">
                  <Td>{fmtTime(r.startedAt)}</Td>
                  <Td right>{num(r.processed)}</Td>
                  <Td right>{r.grayRequests === null ? "н/д" : pct(r.grayRequests, r.processed)}</Td>
                  <Td right>{num(r.joined)}</Td>
                  <Td right>{num(r.newEvents)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
          {verdictTotal > 0 ? (
            <p className="mt-3 text-xs text-zinc-500">
              Вердикты LLM за 7 дней:{" "}
              {stats.verdicts7d
                .map((v) => `${verdictLabel[v.verdict] ?? v.verdict} ${pct(v.count, verdictTotal)}`)
                .join(" · ")}
            </p>
          ) : null}
        </Card>
      </div>

      <Card title="Крупнейшие события" note="Кандидаты в ошибочную склейку: много статей и большой размах по времени.">
        <Table>
          <thead>
            <tr className="border-b border-zinc-200 dark:border-zinc-800">
              <Th>Заголовок</Th>
              <Th right>Статей</Th>
              <Th right>Изданий</Th>
              <Th right>Размах, ч</Th>
            </tr>
          </thead>
          <tbody>
            {stats.topEvents.map((e) => (
              <tr key={e.id} className="border-b border-zinc-100 dark:border-zinc-900">
                <Td>
                  <Link href={`/events/${e.id}`} className="hover:underline">
                    {e.title}
                  </Link>
                </Td>
                <Td right>{num(e.articles)}</Td>
                <Td right>{num(e.sources)}</Td>
                <Td right>{e.spanHours === null ? "—" : num(e.spanHours)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>
    </div>
  );
}

function Sources({ stats }: { stats: AdminStats }) {
  const nowMs = new Date(stats.generatedAt).getTime();
  return (
    <div className="space-y-4">
      <Card title="Приток статей по дням" note="По времени сбора (created_at), UTC, 14 дней. Подсказка при наведении на столбец.">
        <StackedDaily points={stats.daily} />
      </Card>
      <Card title="Источники" note="Красным — источник молчит больше суток.">
        <Table>
          <thead>
            <tr className="border-b border-zinc-200 dark:border-zinc-800">
              <Th>Издание</Th>
              <Th right>Всего</Th>
              <Th right>За 24 ч</Th>
              <Th right>Без текста</Th>
              <Th>Последняя статья</Th>
            </tr>
          </thead>
          <tbody>
            {stats.sources.map((s) => {
              const silent = !s.lastAt || nowMs - new Date(s.lastAt).getTime() > 24 * 3600_000;
              return (
                <tr key={s.name} className="border-b border-zinc-100 dark:border-zinc-900">
                  <Td>{s.name}</Td>
                  <Td right>{num(s.total)}</Td>
                  <Td right>{num(s.last24h)}</Td>
                  <Td right>{pct(s.partial, s.total)}</Td>
                  <Td>
                    <span className={silent ? "text-red-600 dark:text-red-400" : ""}>{fmtAgo(s.lastAt, nowMs)}</span>
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      </Card>
    </div>
  );
}

export default async function AdminPage() {
  await requireAdmin();
  const stats = await getAdminStats();
  const nowMs = new Date(stats.generatedAt).getTime();
  const idleHours = stats.lastAnyRunAt ? (nowMs - new Date(stats.lastAnyRunAt).getTime()) / 3600_000 : null;

  return (
    <main className="mx-auto w-full max-w-5xl flex-1 space-y-6 px-4 py-6 sm:px-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-50">Админка</h1>
          <p className="text-xs text-zinc-500">Данные на {fmtTime(stats.generatedAt)} · кеш до 30 с</p>
        </div>
        <form action={logout}>
          <button
            type="submit"
            className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm text-zinc-700 dark:border-zinc-700 dark:text-zinc-300"
          >
            Выйти
          </button>
        </form>
      </div>

      {idleHours !== null && idleHours > STALE_PIPELINE_HOURS ? (
        <p
          role="status"
          className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300"
        >
          Конвейер не запускался {idleHours.toFixed(1)} ч (последний запуск: {fmtTime(stats.lastAnyRunAt)}).
        </p>
      ) : null}

      <section aria-labelledby="funnel" className="space-y-3">
        <h2 id="funnel" className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
          Воронка обработки
        </h2>
        <Funnels stats={stats} />
      </section>

      <section aria-labelledby="steps" className="space-y-3">
        <h2 id="steps" className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
          Здоровье шагов и квоты
        </h2>
        <Steps stats={stats} />
      </section>

      <section aria-labelledby="quality" className="space-y-3">
        <h2 id="quality" className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
          Качество кластеризации
        </h2>
        <Quality stats={stats} />
      </section>

      <section aria-labelledby="sources" className="space-y-3">
        <h2 id="sources" className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
          Источники и время
        </h2>
        <Sources stats={stats} />
      </section>
    </main>
  );
}
