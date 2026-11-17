"use server";

import { cookies } from "next/headers";
import { notFound, redirect } from "next/navigation";
import {
  ADMIN_COOKIE,
  ADMIN_PATH,
  SESSION_SECONDS,
  createSessionValue,
  isAdminConfigured,
  verifyPassword,
} from "@/lib/admin-auth";

export type LoginState = { error?: string };

// Общий счётчик неудач на процесс (администратор один): после MAX_FAILURES за окно вход
// блокируется до конца окна. На serverless счётчик не общий между экземплярами, это
// замедление перебора, а не гарантия; основная защита — длина пароля (минимум 12 символов).
const WINDOW_MS = 10 * 60 * 1000;
const MAX_FAILURES = 10;
let failures: number[] = [];

function recentFailures(): number[] {
  const since = Date.now() - WINDOW_MS;
  failures = failures.filter((at) => at > since);
  return failures;
}

export async function login(_previous: LoginState, formData: FormData): Promise<LoginState> {
  if (!isAdminConfigured()) notFound();

  if (recentFailures().length >= MAX_FAILURES) {
    return { error: "Слишком много попыток. Подождите несколько минут." };
  }

  const password = formData.get("password");
  if (typeof password !== "string" || !verifyPassword(password)) {
    failures.push(Date.now());
    // Небольшая задержка делает перебор по сети заметно медленнее.
    await new Promise((resolve) => setTimeout(resolve, 800));
    return { error: "Неверный пароль." };
  }

  failures = [];
  const store = await cookies();
  store.set(ADMIN_COOKIE, createSessionValue(), {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict",
    path: ADMIN_PATH,
    maxAge: SESSION_SECONDS,
  });
  redirect(ADMIN_PATH);
}

export async function logout(): Promise<void> {
  const store = await cookies();
  store.delete({ name: ADMIN_COOKIE, path: ADMIN_PATH });
  redirect(`${ADMIN_PATH}/login`);
}
