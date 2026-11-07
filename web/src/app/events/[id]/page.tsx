import Link from "next/link";
import { notFound } from "next/navigation";
import { getEventById, sourceWord } from "@/lib/events";
import { formatDate } from "@/lib/format-date";
import { getEventRender, type CitedClaim, type ClaimBucket } from "@/lib/renders";
import { getMetricChartData, matchMetric, type AllowlistRule, type MetricChartData } from "@/lib/metrics";
import { MetricLineChart } from "@/components/MetricLineChart";
import { getStoryForEvent } from "@/lib/stories";

export const dynamic = "force-dynamic";

function BucketBadge({ bucket, sourceCount }: { bucket: ClaimBucket; sourceCount: number | null }) {
  const label =
    bucket === "confirmed"
      ? `Подтверждено ${sourceCount ?? ""} источниками`.trim()
      : bucket === "disputed"
        ? "Источники расходятся"
        : "Только один источник";
  const tone =
    bucket === "disputed"
      ? "border-amber-300 text-amber-700 dark:border-amber-700 dark:text-amber-400"
      : "border-zinc-300 text-zinc-500 dark:border-zinc-700 dark:text-zinc-500";
  return <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${tone}`}>{label}</span>;
}

async function loadMetricCharts(claims: CitedClaim[]): Promise<Map<string, MetricChartData>> {
  const rulesByCode = new Map<string, AllowlistRule>();
  for (const claim of claims) {
    for (const number of claim.numbers) {
      const rule = matchMetric(number.label, number.unit);
      if (rule) rulesByCode.set(rule.indicatorCode, rule);
    }
  }

  const charts = new Map<string, MetricChartData>();
  for (const [code, rule] of rulesByCode) {
    const data = await getMetricChartData(rule);
    if (data) charts.set(code, data);
  }
  return charts;
}

export default async function EventPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const event = await getEventById(id);

  if (!event) {
    notFound();
  }

  const render = await getEventRender(id, "summary_30s");
  const allClaims = render?.sentences.flatMap((s) => s.claims) ?? [];
  const metricCharts = await loadMetricCharts(allClaims);
  const story = await getStoryForEvent(id);

  return (
    <main className="mx-auto flex max-w-2xl flex-1 flex-col px-4 py-8 sm:px-6">
      <Link
        href="/"
        className="text-sm text-zinc-500 hover:underline dark:text-zinc-400"
      >
        ← На главную
      </Link>

      <article className="mt-4 flex flex-col gap-4">
        <h1 className="text-2xl font-bold leading-tight text-zinc-900 dark:text-zinc-50">
          {event.title}
        </h1>
        <p className="text-sm text-zinc-500 dark:text-zinc-500">
          {event.members.length} {sourceWord(event.members.length)} · обновлено{" "}
          {formatDate(event.updatedAt)}
        </p>

        <div className="flex flex-wrap gap-4">
          <Link
            href={`/events/${id}/compare`}
            className="text-sm text-zinc-600 hover:underline dark:text-zinc-400"
          >
            Сравнить подачи →
          </Link>
          {story && (
            <Link
              href={`/stories/${story.id}`}
              className="text-sm text-zinc-600 hover:underline dark:text-zinc-400"
            >
              Сюжет: {story.title} →
            </Link>
          )}
        </div>

        {render ? (
          <ul className="mt-2 flex flex-col gap-2">
            {render.sentences.map((sentence, i) =>
              sentence.claims.length > 0 ? (
                <li key={i}>
                  <details className="group rounded-lg border border-zinc-200 p-3 open:bg-zinc-50 dark:border-zinc-800 dark:open:bg-zinc-900">
                    <summary className="cursor-pointer list-none text-base leading-relaxed text-zinc-900 [&::-webkit-details-marker]:hidden dark:text-zinc-50">
                      {sentence.text}
                    </summary>
                    <div className="mt-2 flex flex-col gap-3 border-t border-zinc-200 pt-2 dark:border-zinc-800">
                      {sentence.claims.map((c) => (
                        <div key={c.claimId} className="text-sm text-zinc-600 dark:text-zinc-400">
                          {c.bucket && <BucketBadge bucket={c.bucket} sourceCount={c.sourceCount} />}
                          <p className="mt-1">«{c.evidence}»</p>
                          <a
                            href={c.sourceUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="underline"
                          >
                            {c.sourceName}
                          </a>
                          {c.numbers.map((n) => {
                            const rule = matchMetric(n.label, n.unit);
                            const chart = rule ? metricCharts.get(rule.indicatorCode) : null;
                            return chart ? <MetricLineChart key={n.label} {...chart} /> : null;
                          })}
                        </div>
                      ))}
                    </div>
                  </details>
                </li>
              ) : (
                <li key={i} className="px-3 text-base leading-relaxed text-zinc-900 dark:text-zinc-50">
                  {sentence.text}
                </li>
              )
            )}
          </ul>
        ) : (
          <p className="text-sm italic text-zinc-500 dark:text-zinc-500">
            Краткая сводка по этому событию пока не готова.
          </p>
        )}

        <h2 className="mt-6 text-sm font-semibold text-zinc-500 dark:text-zinc-500">Источники</h2>
        <ul className="mt-2 flex flex-col divide-y divide-zinc-200 dark:divide-zinc-800">
          {event.members.map((member) => (
            <li key={member.articleId} className="py-4 first:pt-0">
              <Link href={`/articles/${member.articleId}`} className="group block">
                <p className="text-xs font-medium text-zinc-500 dark:text-zinc-500">
                  {member.sourceName}
                  {member.publishedAt && ` · ${formatDate(member.publishedAt)}`}
                </p>
                <h2 className="mt-1 text-base font-semibold leading-snug text-zinc-900 group-hover:underline dark:text-zinc-50">
                  {member.title}
                </h2>
                {member.lead && (
                  <p className="mt-1 line-clamp-2 text-sm text-zinc-600 dark:text-zinc-400">
                    {member.lead}
                  </p>
                )}
              </Link>
            </li>
          ))}
        </ul>
      </article>
    </main>
  );
}
