import { notFound, redirect } from "next/navigation";
import { ADMIN_PATH, adminState } from "@/lib/admin-auth";
import { LoginForm } from "./LoginForm";

export const dynamic = "force-dynamic";

export default async function AdminLoginPage() {
  const state = await adminState();
  if (state === "off") notFound();
  if (state === "ok") redirect(ADMIN_PATH);

  return (
    <main className="mx-auto w-full max-w-sm flex-1 px-4 py-12">
      <h1 className="mb-6 text-xl font-bold text-zinc-900 dark:text-zinc-50">Вход в админку</h1>
      <LoginForm />
    </main>
  );
}
