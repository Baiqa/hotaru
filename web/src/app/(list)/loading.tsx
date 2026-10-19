// Скелетон вместо спиннера (раздел 2 CLAUDE.md). Живёт в route group (list),
// поэтому не оборачивает /articles/[id] в Suspense — иначе notFound() там
// превращается в мягкий 200 вместо настоящего 404 (см. память по проекту).
export default function Loading() {
  return (
    <main className="mx-auto flex max-w-2xl flex-1 flex-col gap-4 px-4 py-8 sm:px-6">
      <ul className="flex flex-col divide-y divide-zinc-200 dark:divide-zinc-800">
        {Array.from({ length: 5 }).map((_, i) => (
          <li key={i} className="flex flex-col gap-2 py-5 first:pt-0 animate-pulse">
            <div className="h-5 w-4/5 rounded bg-zinc-200 dark:bg-zinc-800" />
            <div className="h-4 w-full rounded bg-zinc-200 dark:bg-zinc-800" />
            <div className="h-3 w-1/3 rounded bg-zinc-200 dark:bg-zinc-800" />
          </li>
        ))}
      </ul>
    </main>
  );
}
