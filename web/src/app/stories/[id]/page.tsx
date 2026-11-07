import Link from "next/link";
import { notFound } from "next/navigation";
import { getStoryById } from "@/lib/stories";
import { formatDate } from "@/lib/format-date";

export const dynamic = "force-dynamic";

export default async function StoryPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const story = await getStoryById(id);

  if (!story) {
    notFound();
  }

  return (
    <main className="mx-auto flex max-w-2xl flex-1 flex-col px-4 py-8 sm:px-6">
      <Link href="/" className="text-sm text-zinc-500 hover:underline dark:text-zinc-400">
        ← На главную
      </Link>

      <article className="mt-4 flex flex-col gap-4">
        <h1 className="text-2xl font-bold leading-tight text-zinc-900 dark:text-zinc-50">
          {story.title}
        </h1>
        <p className="text-sm text-zinc-500 dark:text-zinc-500">
          {story.events.length} {story.events.length === 1 ? "событие" : "событий"} в сюжете
        </p>

        {/* Вертикальный таймлайн, не горизонтальная метро-схема — единственное
            жёсткое требование CLAUDE.md §5 здесь — читаемость на телефоне без
            горизонтального скролла, вертикальный список даёт это при любом
            числе событий. Хронология сверху вниз (старое -> новое), как
            рассказ "решение -> реакция -> последствие". */}
        <ol className="relative mt-2 flex flex-col gap-6 border-l border-zinc-200 pl-6 dark:border-zinc-800">
          {story.events.map((event) => (
            <li key={event.eventId} className="relative">
              <span className="absolute -left-[29px] top-1.5 h-3 w-3 rounded-full border-2 border-zinc-400 bg-white dark:border-zinc-600 dark:bg-black" />
              <p className="text-xs text-zinc-500 dark:text-zinc-500">{formatDate(event.startedAt)}</p>
              <Link
                href={`/events/${event.eventId}`}
                className="mt-1 block text-base font-medium leading-snug text-zinc-900 hover:underline dark:text-zinc-50"
              >
                {event.title}
              </Link>
            </li>
          ))}
        </ol>
      </article>
    </main>
  );
}
