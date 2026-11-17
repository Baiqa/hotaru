import "server-only";
import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";
import { notFound, redirect } from "next/navigation";

// Защита страницы /admin: один общий пароль из переменной окружения.
// Закрыто по умолчанию: без ADMIN_PASSWORD (не короче 12 символов) и ADMIN_DATABASE_URL
// все адреса /admin отвечают 404, как будто страницы нет.

export const ADMIN_COOKIE = "hotaru_admin";
export const ADMIN_PATH = "/admin";
export const SESSION_SECONDS = 12 * 60 * 60;
const MIN_PASSWORD_LENGTH = 12;

function configuredPassword(): string | null {
  const password = process.env.ADMIN_PASSWORD;
  return password && password.length >= MIN_PASSWORD_LENGTH ? password : null;
}

export function isAdminConfigured(): boolean {
  return configuredPassword() !== null && Boolean(process.env.ADMIN_DATABASE_URL);
}

function signingSecret(): string {
  return process.env.ADMIN_SESSION_SECRET || configuredPassword() || "";
}

function sign(expiresAt: number): string {
  return createHmac("sha256", signingSecret()).update(`v1.${expiresAt}`).digest("hex");
}

// Сравнение за постоянное время: оба значения сначала приводятся к хешу одной длины.
function safeEqual(a: string, b: string): boolean {
  const ha = createHash("sha256").update(a).digest();
  const hb = createHash("sha256").update(b).digest();
  return timingSafeEqual(ha, hb);
}

export function verifyPassword(input: string): boolean {
  const password = configuredPassword();
  if (!password) return false;
  return safeEqual(input, password);
}

export function createSessionValue(): string {
  const expiresAt = Date.now() + SESSION_SECONDS * 1000;
  return `${expiresAt}.${sign(expiresAt)}`;
}

function isValidSession(value: string | undefined): boolean {
  if (!value || !isAdminConfigured()) return false;
  const [expires, signature, ...rest] = value.split(".");
  if (!expires || !signature || rest.length > 0) return false;
  const expiresAt = Number(expires);
  if (!Number.isFinite(expiresAt) || expiresAt < Date.now()) return false;
  return safeEqual(signature, sign(expiresAt));
}

export type AdminState = "off" | "anonymous" | "ok";

export async function adminState(): Promise<AdminState> {
  if (!isAdminConfigured()) return "off";
  const store = await cookies();
  return isValidSession(store.get(ADMIN_COOKIE)?.value) ? "ok" : "anonymous";
}

// Проверка вызывается в самой странице и в загрузчике данных, а не только в layout:
// layout не перерисовывается при переходах и не может быть единственной защитой.
export async function requireAdmin(): Promise<void> {
  const state = await adminState();
  if (state === "off") notFound();
  if (state === "anonymous") redirect(`${ADMIN_PATH}/login`);
}
