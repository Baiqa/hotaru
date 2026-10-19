import { createClient } from "@supabase/supabase-js";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

if (!url || !anonKey) {
  throw new Error(
    "NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY не заданы. " +
      "Скопируйте web/.env.local.example в web/.env.local."
  );
}

// Анонимный read-only ключ, ограниченный RLS-политиками (публичное чтение
// sources/articles) — см. supabase/migrations. Для записи используется
// service_role только из worker/, сюда он попадать не должен.
export const supabase = createClient(url, anonKey);
