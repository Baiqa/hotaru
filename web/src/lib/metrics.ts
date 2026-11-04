import { supabase } from "./supabase";

type AllowlistCountry = { iso3: string; nameRu: string };

export type AllowlistRule = {
  indicatorCode: string;
  countries: AllowlistCountry[];
  test: (label: string | null, unit: string | null) => boolean;
};

// Этап-3 MVP: страна не выводится из сущностей события (claim_numbers не связан
// с конкретной страной напрямую) — захардкожен фиксированный набор, Казахстан и
// Россия, т.к. весь корпус CLAUDE.md заточен под kk/ru/en-новости этих рынков.
// Обе линии показываются, если для обеих нашлись точки. Правило — не модель,
// по образцу CLAUDE.md §3.5 "тип графика по правилу, не моделью": проверка реальных
// claim_numbers (2026-11-05) показала, что подавляющее большинство чисел в
// корпусе — не экономические (день месяца, возраст, погибшие...), поэтому
// расширять allowlist стоит только по новым реально встреченным меткам.
const ALLOWLIST: AllowlistRule[] = [
  {
    indicatorCode: "FP.CPI.TOTL.ZG",
    countries: [
      { iso3: "KAZ", nameRu: "Казахстан" },
      { iso3: "RUS", nameRu: "Россия" },
    ],
    test: (label, unit) => !!label && /инфляц/i.test(label) && unit === "%",
  },
];

export function matchMetric(label: string | null, unit: string | null): AllowlistRule | null {
  return ALLOWLIST.find((rule) => rule.test(label, unit)) ?? null;
}

export type MetricLine = {
  countryName: string;
  points: { period: string; value: number }[];
};

export type MetricChartData = {
  unit: string;
  sourceName: string;
  sourceUrl: string;
  lines: MetricLine[];
};

type MetricPointRow = { period: string; value: number };
type MetricSeriesRow = {
  key: string;
  unit: string;
  source_name: string;
  source_url: string;
  metric_points: MetricPointRow[] | null;
};

export async function getMetricChartData(rule: AllowlistRule): Promise<MetricChartData | null> {
  const keys = rule.countries.map((c) => `${rule.indicatorCode}:${c.iso3}`);

  const { data, error } = await supabase
    .from("metric_series")
    .select("key, unit, source_name, source_url, metric_points(period, value)")
    .in("key", keys);

  if (error) {
    throw new Error(`Не удалось загрузить метрики: ${error.message}`);
  }
  if (!data || data.length === 0) {
    return null;
  }

  const rows = data as unknown as MetricSeriesRow[];
  const lines = rows
    .map((row) => {
      const country = rule.countries.find((c) => row.key.endsWith(`:${c.iso3}`));
      if (!country) return null;
      const points = [...(row.metric_points ?? [])].sort((a, b) => a.period.localeCompare(b.period));
      if (points.length === 0) return null;
      return { countryName: country.nameRu, points };
    })
    .filter((l): l is MetricLine => l !== null);

  if (lines.length === 0) {
    return null;
  }

  const first = rows[0];
  return {
    unit: first.unit,
    sourceName: first.source_name,
    sourceUrl: first.source_url,
    lines,
  };
}
