import "server-only";
import { Pool } from "pg";

// Серверное подключение страницы /admin к Postgres ролью admin_reader (только SELECT,
// см. supabase/migrations/20261117120000_admin_reader_role.sql). НЕ service_role и не
// строка подключения воркера: утечка этого доступа не даёт ничего записать.
//
// Пул держим на globalThis, иначе в dev каждая перезагрузка модуля открывала бы новый.
const globalForPool = globalThis as unknown as { __hotaruAdminPool?: Pool };

function createPool(): Pool {
  const url = process.env.ADMIN_DATABASE_URL;
  if (!url) throw new Error("ADMIN_DATABASE_URL не задан");

  const pool = new Pool({
    connectionString: url,
    max: 3,
    idleTimeoutMillis: 10_000,
    connectionTimeoutMillis: 8_000,
    // Пулер Supabase отдаёт сертификат, которого нет в хранилище доверенных Node.js;
    // канал при этом шифруется, но имя сервера не проверяется. Для read-only роли с
    // ограниченными колонками это приемлемо; пароль не показывается на странице.
    ssl: { rejectUnauthorized: false },
  });

  pool.on("error", (error) => {
    console.error("admin-db: ошибка простаивающего соединения", error.message);
  });
  return pool;
}

export function adminPool(): Pool {
  globalForPool.__hotaruAdminPool ??= createPool();
  return globalForPool.__hotaruAdminPool;
}

// Каждый запрос идёт в транзакции `begin read only` с коротким таймаутом: запись
// невозможна на уровне транзакции независимо от прав роли (двойная защита поверх admin_reader).
// Числа в SQL приводим к ::int, чтобы pg не отдавал bigint строками.
export async function query<T>(sql: string, params: unknown[] = []): Promise<T[]> {
  const client = await adminPool().connect();
  try {
    await client.query("begin read only");
    await client.query("set local statement_timeout = '15s'");
    const result = await client.query(sql, params);
    await client.query("commit");
    return result.rows as T[];
  } catch (error) {
    await client.query("rollback").catch(() => {});
    throw error;
  } finally {
    client.release();
  }
}
