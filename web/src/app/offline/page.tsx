import Link from "next/link";

export const metadata = {
  title: "Нет сети — Hotaru",
  robots: { index: false },
};

// Показывается Service Worker'ом, когда нужной страницы нет в сохранённых и нет сети.
export default function OfflinePage() {
  return (
    <main className="mx-auto flex max-w-2xl flex-1 flex-col items-start gap-3 px-4 py-8 sm:px-6">
      <h1 className="text-lg font-semibold text-zinc-900 dark:text-zinc-50">Нет сети</h1>
      <p className="text-base text-zinc-700 dark:text-zinc-300">
        Эта страница не сохранена. Откройте её, пока есть сеть: после этого она будет доступна без
        интернета.
      </p>
      <Link href="/" className="text-sm text-zinc-500 hover:underline dark:text-zinc-400">
        ← На главную
      </Link>
    </main>
  );
}
