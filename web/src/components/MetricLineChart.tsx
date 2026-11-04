import type { MetricChartData } from "@/lib/metrics";

const WIDTH = 400;
const HEIGHT = 140;
const PAD = 24;
// zinc-700, amber-700 — максимум 2 линии в этом MVP (Казахстан + Россия).
const PALETTE = ["#3f3f46", "#b45309"];

// Чистая функция -> inline SVG, серверный компонент без клиентского JS: для
// ~15 годовых точек на 2 страны ручная min/max/scale арифметика проще, чем
// тащить новую зависимость (recharts/visx), которой пока нет в package.json —
// см. CLAUDE.md §2 "D3/свои SVG-компоненты", без обязательности сторонних либ.
export function MetricLineChart({ unit, sourceName, sourceUrl, lines }: MetricChartData) {
  const allPeriods = [...new Set(lines.flatMap((l) => l.points.map((p) => p.period)))].sort();
  const allValues = lines.flatMap((l) => l.points.map((p) => p.value));
  const min = Math.min(...allValues);
  const max = Math.max(...allValues);

  const x = (period: string) => {
    const idx = allPeriods.indexOf(period);
    const span = allPeriods.length - 1 || 1;
    return PAD + (idx / span) * (WIDTH - 2 * PAD);
  };
  const y = (value: number) => {
    const range = max - min || 1;
    return HEIGHT - PAD - ((value - min) / range) * (HEIGHT - 2 * PAD);
  };

  return (
    <figure className="mt-2">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="w-full text-zinc-300 dark:text-zinc-700">
        <line x1={PAD} y1={HEIGHT - PAD} x2={WIDTH - PAD} y2={HEIGHT - PAD} stroke="currentColor" strokeWidth="1" />
        {lines.map((line, i) => (
          <polyline
            key={line.countryName}
            fill="none"
            stroke={PALETTE[i % PALETTE.length]}
            strokeWidth="2"
            points={line.points.map((p) => `${x(p.period)},${y(p.value)}`).join(" ")}
          />
        ))}
      </svg>
      <figcaption className="mt-1 flex flex-wrap gap-3 text-xs text-zinc-500 dark:text-zinc-500">
        {lines.map((line, i) => (
          <span key={line.countryName} style={{ color: PALETTE[i % PALETTE.length] }}>
            {line.countryName}
          </span>
        ))}
        <span>
          {unit} ·{" "}
          <a href={sourceUrl} target="_blank" rel="noopener noreferrer" className="underline">
            {sourceName}
          </a>
        </span>
      </figcaption>
    </figure>
  );
}
