import type { DailyPoint } from "@/lib/admin-stats";

// Компоненты страницы /admin: серверные, без состояния, на чистом SVG/CSS.

export const num = (value: number): string => value.toLocaleString("ru-RU");

export function pct(part: number, whole: number): string {
  if (whole <= 0) return "—";
  const value = (part / whole) * 100;
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)}%`;
}

const timeFormat = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "UTC",
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

export function fmtTime(iso: string | null): string {
  return iso ? `${timeFormat.format(new Date(iso))} UTC` : "—";
}

export function fmtDuration(startIso: string, endIso: string | null): string {
  if (!endIso) return "—";
  const seconds = Math.max(0, Math.round((new Date(endIso).getTime() - new Date(startIso).getTime()) / 1000));
  if (seconds < 90) return `${seconds} с`;
  const minutes = seconds / 60;
  return minutes < 90 ? `${Math.round(minutes)} мин` : `${(minutes / 60).toFixed(1)} ч`;
}

export function fmtAgo(iso: string | null, nowMs: number): string {
  if (!iso) return "—";
  const minutes = Math.max(0, Math.round((nowMs - new Date(iso).getTime()) / 60000));
  if (minutes < 90) return `${minutes} мин назад`;
  const hours = minutes / 60;
  return hours < 48 ? `${hours.toFixed(1)} ч назад` : `${Math.round(hours / 24)} дн назад`;
}

export function Card({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
      <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-50">{title}</h3>
      {note ? <p className="mt-0.5 text-xs text-zinc-500">{note}</p> : null}
      <div className="mt-3">{children}</div>
    </section>
  );
}

export type BarRow = {
  label: string;
  value: number;
  // Значение, от которого считается ширина полосы и процент.
  of: number;
  // Сколько на этом шаге ещё ждёт обработки (необязательно).
  waiting?: number;
  hint?: string;
};

export function BarList({ rows, tone = "bg-sky-500" }: { rows: BarRow[]; tone?: string }) {
  return (
    <ul className="space-y-2.5">
      {rows.map((row) => {
        const width = row.of > 0 ? Math.min(100, (row.value / row.of) * 100) : 0;
        return (
          <li key={row.label}>
            <div className="flex items-baseline justify-between gap-3 text-sm">
              <span className="text-zinc-700 dark:text-zinc-300">{row.label}</span>
              <span className="shrink-0 tabular-nums text-zinc-900 dark:text-zinc-50">
                {num(row.value)} <span className="text-xs text-zinc-500">· {pct(row.value, row.of)}</span>
              </span>
            </div>
            <div
              className="mt-1 h-2 overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-800"
              role="img"
              aria-label={`${row.label}: ${num(row.value)} из ${num(row.of)}`}
            >
              <div className={`h-full rounded-full ${tone}`} style={{ width: `${width}%` }} />
            </div>
            {row.waiting !== undefined || row.hint ? (
              <p className="mt-0.5 text-xs text-zinc-500">
                {row.waiting !== undefined ? `ждут: ${num(row.waiting)}` : null}
                {row.waiting !== undefined && row.hint ? " · " : null}
                {row.hint}
              </p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

const STATUS_TONE: Record<string, string> = {
  ok: "border-emerald-300 text-emerald-700 dark:border-emerald-800 dark:text-emerald-400",
  failed: "border-red-300 text-red-700 dark:border-red-800 dark:text-red-400",
  running: "border-amber-300 text-amber-700 dark:border-amber-700 dark:text-amber-400",
};

const STATUS_LABEL: Record<string, string> = { ok: "ок", failed: "сбой", running: "идёт" };

export function StatusPill({ status, stale = false }: { status: string; stale?: boolean }) {
  const tone = stale ? STATUS_TONE.failed : (STATUS_TONE[status] ?? STATUS_TONE.running);
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${tone}`}>
      {stale ? "завис" : (STATUS_LABEL[status] ?? status)}
    </span>
  );
}

// Палитра различимых цветов для изданий (с запасом на новые источники).
const SOURCE_COLORS = [
  "#38bdf8",
  "#f472b6",
  "#a3e635",
  "#fbbf24",
  "#a78bfa",
  "#2dd4bf",
  "#fb7185",
  "#60a5fa",
  "#f97316",
  "#94a3b8",
  "#4ade80",
  "#e879f9",
];

export function StackedDaily({ points }: { points: DailyPoint[] }) {
  const days = [...new Set(points.map((p) => p.day))].sort();
  const sources = [...new Set(points.map((p) => p.source))].sort();
  if (days.length === 0) return <p className="text-sm text-zinc-500">Нет данных за 14 дней.</p>;

  const byDay = new Map<string, Map<string, number>>();
  for (const p of points) {
    const row = byDay.get(p.day) ?? new Map<string, number>();
    row.set(p.source, (row.get(p.source) ?? 0) + p.count);
    byDay.set(p.day, row);
  }
  const totals = days.map((d) => [...(byDay.get(d)?.values() ?? [])].reduce((s, v) => s + v, 0));
  const max = Math.max(...totals, 1);

  const width = 640;
  const height = 190;
  const padLeft = 34;
  const padBottom = 22;
  const innerWidth = width - padLeft - 4;
  const innerHeight = height - padBottom - 8;
  const slot = innerWidth / days.length;
  const barWidth = Math.max(4, slot * 0.7);

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="Приток статей по дням и изданиям">
        {[0, 0.5, 1].map((t) => {
          const y = 8 + innerHeight * (1 - t);
          return (
            <g key={t}>
              <line x1={padLeft} x2={width - 4} y1={y} y2={y} className="stroke-zinc-200 dark:stroke-zinc-800" />
              <text x={padLeft - 4} y={y + 3} textAnchor="end" className="fill-zinc-500" fontSize="9">
                {Math.round(max * t)}
              </text>
            </g>
          );
        })}
        {days.map((day, i) => {
          let offset = 0;
          const x = padLeft + i * slot + (slot - barWidth) / 2;
          return (
            <g key={day}>
              {sources.map((source, si) => {
                const count = byDay.get(day)?.get(source) ?? 0;
                if (count === 0) return null;
                const h = (count / max) * innerHeight;
                const y = 8 + innerHeight - offset - h;
                offset += h;
                return (
                  <rect key={source} x={x} y={y} width={barWidth} height={h} fill={SOURCE_COLORS[si % SOURCE_COLORS.length]}>
                    <title>{`${day} · ${source}: ${count}`}</title>
                  </rect>
                );
              })}
              <text x={x + barWidth / 2} y={height - 8} textAnchor="middle" className="fill-zinc-500" fontSize="9">
                {day.slice(8)}
              </text>
            </g>
          );
        })}
      </svg>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-600 dark:text-zinc-400">
        {sources.map((source, si) => (
          <li key={source} className="flex items-center gap-1.5">
            <span
              className="inline-block h-2.5 w-2.5 rounded-sm"
              style={{ backgroundColor: SOURCE_COLORS[si % SOURCE_COLORS.length] }}
              aria-hidden
            />
            {source}
          </li>
        ))}
      </ul>
    </div>
  );
}
