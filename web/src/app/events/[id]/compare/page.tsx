import Link from "next/link";
import { notFound } from "next/navigation";
import { getEventById } from "@/lib/events";
import { getEventComparison, type ComparisonFact } from "@/lib/comparison";

export const dynamic = "force-dynamic";

function FactRow({ fact, sources }: { fact: ComparisonFact; sources: string[] }) {
  const isDisputed = fact.bucket === "disputed";

  return (
    <details className="group" style={{ display: "contents" }}>
      <summary
        className="cursor-pointer list-none [&::-webkit-details-marker]:hidden"
        style={{ display: "contents" }}
      >
        <div
          className={`border-t border-zinc-200 px-2 py-2 text-sm text-zinc-900 group-open:bg-zinc-50 dark:border-zinc-800 dark:text-zinc-50 dark:group-open:bg-zinc-900 ${
            isDisputed ? "bg-amber-50 dark:bg-amber-950/30" : ""
          }`}
        >
          {fact.representativeText}
          {isDisputed && (
            <span className="ml-2 inline-block rounded-full border border-amber-300 px-2 py-0.5 text-xs font-medium text-amber-700 dark:border-amber-700 dark:text-amber-400">
              Расходится
            </span>
          )}
        </div>
        {sources.map((source) => (
          <div
            key={source}
            className={`border-t border-zinc-200 px-2 py-2 text-center text-sm text-zinc-500 group-open:bg-zinc-50 dark:border-zinc-800 dark:text-zinc-500 dark:group-open:bg-zinc-900 ${
              isDisputed ? "bg-amber-50 dark:bg-amber-950/30" : ""
            }`}
          >
            {fact.sourceNames.includes(source) ? "✓" : "—"}
          </div>
        ))}
      </summary>
      <div
        style={{ gridColumn: "1 / -1" }}
        className="flex flex-col gap-2 border-t border-zinc-200 bg-zinc-50 px-3 py-3 text-sm dark:border-zinc-800 dark:bg-zinc-900"
      >
        {fact.bucket === "confirmed" && (
          <>
            <p className="text-zinc-700 dark:text-zinc-300">«{fact.representativeText}»</p>
            <p className="text-xs text-zinc-500 dark:text-zinc-500">
              Подтвердили: {fact.sourceNames.join(", ")}
            </p>
          </>
        )}
        {fact.bucket === "unique" && fact.members[0] && (
          <p className="text-zinc-700 dark:text-zinc-300">
            Только <span className="font-medium">{fact.members[0].sourceName}</span> упоминает, что «
            {fact.members[0].text}»
          </p>
        )}
        {isDisputed &&
          fact.members.map((m, i) => (
            <p key={i} className="text-zinc-700 dark:text-zinc-300">
              <span className="font-medium">{m.sourceName}</span>: «{m.text}»
            </p>
          ))}
        <div className="flex flex-wrap gap-3 pt-1">
          {fact.members.map((m, i) => (
            <a
              key={i}
              href={m.sourceUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-zinc-500 underline dark:text-zinc-500"
            >
              {m.sourceName}
            </a>
          ))}
        </div>
      </div>
    </details>
  );
}

export default async function ComparePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const event = await getEventById(id);

  if (!event) {
    notFound();
  }

  const comparison = await getEventComparison(id);

  return (
    <main className="mx-auto flex max-w-2xl flex-1 flex-col px-4 py-8 sm:px-6">
      <Link href={`/events/${id}`} className="text-sm text-zinc-500 hover:underline dark:text-zinc-400">
        ← К событию
      </Link>

      <article className="mt-4 flex flex-col gap-4">
        <h1 className="text-2xl font-bold leading-tight text-zinc-900 dark:text-zinc-50">
          {event.title}
        </h1>

        <div className="flex flex-wrap gap-3">
          {event.members.map((m) => (
            <a
              key={m.articleId}
              href={`/articles/${m.articleId}`}
              className="min-w-[160px] flex-1 rounded-lg border border-zinc-200 p-3 text-sm hover:border-zinc-300 dark:border-zinc-800 dark:hover:border-zinc-700"
            >
              <p className="text-xs font-medium text-zinc-500 dark:text-zinc-500">{m.sourceName}</p>
              <p className="mt-1 leading-snug text-zinc-900 dark:text-zinc-50">{m.title}</p>
            </a>
          ))}
        </div>

        {comparison ? (
          <div className="mt-2 overflow-x-auto">
            <div
              className="inline-grid min-w-full"
              style={{
                gridTemplateColumns: `minmax(220px, 1fr) repeat(${comparison.sources.length}, minmax(90px, auto))`,
              }}
            >
              <div className="px-2 py-2 text-xs font-semibold text-zinc-500 dark:text-zinc-500">Факт</div>
              {comparison.sources.map((source) => (
                <div
                  key={source}
                  className="truncate px-2 py-2 text-xs font-semibold text-zinc-500 dark:text-zinc-500"
                  title={source}
                >
                  {source}
                </div>
              ))}
              {comparison.facts.map((fact) => (
                <FactRow key={fact.groupId} fact={fact} sources={comparison.sources} />
              ))}
            </div>
          </div>
        ) : (
          <p className="text-sm italic text-zinc-500 dark:text-zinc-500">
            Пока недостаточно сгруппированных фактов, чтобы сравнить подачи — заходите позже.
          </p>
        )}
      </article>
    </main>
  );
}
