import Link from "next/link";
import { notFound } from "next/navigation";
import { getEventById, sourceWord } from "@/lib/events";
import { formatDate } from "@/lib/format-date";

export const dynamic = "force-dynamic";

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
