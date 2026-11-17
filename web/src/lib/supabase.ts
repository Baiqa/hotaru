import { createClient } from "@supabase/supabase-js";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

if (!url || !anonKey) {
  throw new Error(
    "NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY не заданы. " +
      "Скопируйте web/.env.example в web/.env.local."
  );
}

// Анонимный read-only ключ, ограниченный RLS-политиками (публичное чтение
// sources/articles) — см. supabase/migrations и docs/rbac.md. Пишет в БД только воркер
// по прямой строке подключения DATABASE_URL (не через API); service_role не используется
// нигде, а сюда не должен попадать ни он, ни строка воркера.
export const supabase = createClient(url, anonKey);
