import Link from "next/link";
import { listEvents, sourceWord } from "@/lib/events";
import { formatDate } from "@/lib/format-date";

// Без этого Next.js выполняет запрос один раз при `next build` и замораживает
// список — новые события из кроновых прогонов воркера не будут появляться.
export const dynamic = "force-dynamic";

export default async function Home() {
  const events = await listEvents();

  return (
    <main className="mx-auto flex max-w-2xl flex-1 flex-col gap-4 px-4 py-8 sm:px-6">
      <ul className="flex flex-col divide-y divide-zinc-200 dark:divide-zinc-800">
        {events.map((event) => (
          <li key={event.id} className="py-5 first:pt-0">
            <Link href={`/events/${event.id}`} className="group block">
              <h2 className="text-lg font-semibold leading-snug text-zinc-900 group-hover:underline dark:text-zinc-50">
                {event.title}
              </h2>
              {event.sourceNames.length > 0 && (
                <p className="mt-1.5 line-clamp-2 text-sm text-zinc-600 dark:text-zinc-400">
                  {event.sourceNames.join(" · ")}
                </p>
              )}
              <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-500">
                {event.articleCount} {sourceWord(event.articleCount)}
                {` · ${formatDate(event.updatedAt)}`}
              </p>
            </Link>
          </li>
        ))}
      </ul>
    </main>
  );
}
