import Link from "next/link";
import { notFound } from "next/navigation";
import { getArticleById } from "@/lib/articles";
import { getEventIdForArticle } from "@/lib/events";
import { formatDate } from "@/lib/format-date";

export const dynamic = "force-dynamic";

export default async function ArticlePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const article = await getArticleById(id);

  if (!article) {
    notFound();
  }

  const eventId = await getEventIdForArticle(id);

  return (
    <main className="mx-auto flex max-w-2xl flex-1 flex-col px-4 py-8 sm:px-6">
      <div className="flex items-center gap-4">
        <Link
          href="/"
          className="text-sm text-zinc-500 hover:underline dark:text-zinc-400"
        >
          ← На главную
        </Link>
        {eventId && (
          <Link
            href={`/events/${eventId}`}
            className="text-sm text-zinc-500 hover:underline dark:text-zinc-400"
          >
            ← К событию
          </Link>
        )}
      </div>

      <article className="mt-4 flex flex-col gap-4">
        <h1 className="text-2xl font-bold leading-tight text-zinc-900 dark:text-zinc-50">
          {article.title}
        </h1>
        <p className="text-sm text-zinc-500 dark:text-zinc-500">
          {article.sourceName}
          {article.publishedAt && ` · ${formatDate(article.publishedAt)}`}
        </p>
        {article.lead && (
          <p className="text-base leading-relaxed text-zinc-700 dark:text-zinc-300">
            {article.lead}
          </p>
        )}
        <a
          href={article.sourceUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-2 w-fit rounded-full border border-zinc-300 px-4 py-2 text-sm font-medium text-zinc-900 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-50 dark:hover:bg-zinc-900"
        >
          Читать оригинал: {article.sourceName} →
        </a>
      </article>
    </main>
  );
}
