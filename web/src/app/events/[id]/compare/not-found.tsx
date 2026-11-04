import Link from "next/link";

export default function CompareNotFound() {
  return (
    <main className="mx-auto flex max-w-2xl flex-1 flex-col items-start gap-3 px-4 py-8 sm:px-6">
      <p className="text-base text-zinc-700 dark:text-zinc-300">
        Событие не найдено.
      </p>
      <Link href="/" className="text-sm text-zinc-500 hover:underline dark:text-zinc-400">
        ← На главную
      </Link>
    </main>
  );
}
