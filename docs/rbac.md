# Матрица ролей и прав доступа (RBAC)

Состояние на 2026-11-18, после миграций `20261117191848` и `20261117192143`. Права в БД сверены с живой базой
(каталог Postgres), а не только с миграциями.
Если вы меняете права, меняйте их миграцией в `supabase/migrations/` и обновляйте этот файл.

## Принцип

Приложение без пользовательских аккаунтов: посетитель только читает. Всё, что пишет в БД, живёт вне
браузера (воркер). Права ограничены на четырёх уровнях:

1. **Строки:** RLS включён на всех 20 таблицах схемы `public`.
2. **Колонки:** анониму отозван табличный `SELECT` там, где есть закрытые поля (`articles`, `claims`, `events`,
   `claim_groups`, `stories`), выдан список колонок.
3. **Привилегии:** `anon` и `authenticated` имеют только `SELECT`; `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`,
   `REFERENCES`, `TRIGGER` и `MAINTAIN` отозваны на всех таблицах `public`.
4. **Роль:** страница `/admin` ходит в БД не владельцем, а отдельной ролью `admin_reader`.

## Роли

| Роль | Кто это | Как входит | Что может |
|---|---|---|---|
| **Посетитель** (`anon`) | Любой, у кого есть публичный URL | Анонимный ключ `NEXT_PUBLIC_SUPABASE_ANON_KEY` из браузера | Читать публичный контент (см. матрицу). Писать нельзя |
| **Администратор** | Владелец продукта | Пароль `ADMIN_PASSWORD` на `/admin`, cookie 12 ч | Смотреть служебную статистику. Только чтение |
| **`admin_reader`** (роль БД) | Серверная часть Next.js для `/admin` | `ADMIN_DATABASE_URL`, только на сервере | `SELECT` по перечисленным колонкам, транзакции `read only`, таймаут 15 с, до 5 соединений |
| **Воркер** | Python-процесс (локально или GitHub Actions) | `DATABASE_URL` (Session pooler) | Полный доступ. По шаблону строки это роль `postgres`, она обходит RLS |
| **Оператор** | Владелец аккаунтов Supabase, Vercel, GitHub | Свои аккаунты | Управляет секретами, миграциями, деплоем |

Не используются: `authenticated` (Supabase Auth в приложении не подключён; у этой роли те же права, что у `anon`)
и `service_role` (нигде в коде и конфигурации).

## Матрица доступа к данным

`R` — чтение, `—` — нет доступа, `R*` — чтение части колонок. Запись у `anon` и `admin_reader` отсутствует везде.
Воркер (`postgres`) читает и пишет всё, поэтому в таблицу не вынесен.

### Публичный контент

| Таблица | Посетитель | `admin_reader` | Примечание |
|---|---|---|---|
| `sources` | R | R* (без `created_at`, `updated_at`) | |
| `articles` | R* | R* | Посетитель: `id, source_id, url, canonical_url, title, lead, published_at, fetch_status, created_at`. **Полный текст `body`, `embedding`, `content_hash` закрыты.** `admin_reader` видит служебные поля обработки, но не `url`, `lead`, `body` |
| `claims` | R* | R* | Посетитель: `id, article_id, text, type, evidence, created_at` (без `embedding`). `admin_reader`: без `text` и `evidence`, но с `embedding` |
| `events`, `stories` | R* (все колонки, кроме `centroid`) | R* | Колонки перечислены явно, новые нужно выдавать миграцией |
| `event_articles`, `story_events` | R | R | |
| `claim_groups` | R* (без `centroid`) | R* | То же |
| `claim_group_members` | R | R* | |
| `claim_numbers` | R | — | |
| `renders` | R (включая `body`) | R* (без `body`) | `body` — наш собственный синтез, а не чужой текст |
| `render_claims` | R | — | |
| `entities` | R | R* (`id`, `type`) | |
| `entity_aliases` | R | — | |
| `article_entities` | R | R | |
| `metric_series`, `metric_points` | R | — | Исторические ряды World Bank |

### Служебные таблицы воркера

| Таблица | Посетитель | `admin_reader` | Как закрыто |
|---|---|---|---|
| `pipeline_runs` | — | R | RLS без политик для `anon` и `revoke all` |
| `gray_zone_verdicts` | — | R | RLS: политика только для `admin_reader`, плюс `revoke all` для `anon` и `authenticated` |
| `monitoring_checks` | — | — | RLS без политик, плюс `revoke all` для `anon` и `authenticated` |

## Матрица страниц и действий

| Адрес / действие | Посетитель | Администратор | Реализация |
|---|---|---|---|
| `/`, `/events/[id]`, `/events/[id]/compare`, `/stories/[id]`, `/articles/[id]`, `/offline` | да | да | Чтение через анонимный ключ |
| `/admin/login` | 404, если админка не настроена; иначе форма | редирект на `/admin`, если уже вошёл | `adminState()` |
| `/admin` | 404 или редирект на логин | да | `requireAdmin()` в самой странице и в загрузчике данных, а не только в `layout` |
| Вход | — | пароль; 800 мс задержка при ошибке; 10 неудач за 10 минут блокируют вход | `login()` в `admin/login/actions.ts` |
| Выход | — | удаление cookie | `logout()` |

Админка **закрыта по умолчанию**: без `ADMIN_PASSWORD` (≥ 12 символов) и `ADMIN_DATABASE_URL` все адреса `/admin` отвечают 404.
Cookie `hotaru_admin`: `httpOnly`, `sameSite=strict`, `secure` в продакшене, путь `/admin`, подпись HMAC-SHA256, сравнение за постоянное время.
Ответы `/admin/*` идут с `Cache-Control: no-store` и `X-Robots-Tag: noindex, nofollow`; Service Worker их не кеширует (белый список страниц).

## Секреты

| Секрет | Кто использует | Где хранится | Утечка даёт |
|---|---|---|---|
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Браузер | Vercel env, `web/.env.local` | Ничего сверх публичного чтения. Это нормально, ключ публичный по построению |
| `ADMIN_PASSWORD` | Сервер Next.js | Vercel env, `web/.env.local` | Вход в `/admin` (только просмотр) |
| `ADMIN_SESSION_SECRET` | Сервер Next.js | то же | Возможность подделать cookie админа |
| `ADMIN_DATABASE_URL` | Сервер Next.js | то же | Только чтение перечисленных колонок |
| `DATABASE_URL` | Воркер | `worker/.env`, GitHub Secrets | **Полный доступ к БД** |
| `GOOGLE_API_KEY`, `GROQ_API_KEY` | Воркер | то же | Трата квоты LLM |

Реальные `.env` и `.env.local` не попадают в git (проверено по `git ls-files`). Репозиторий публичный: workflow
запускаются только по расписанию и вручную, без `pull_request`, чтобы секреты не доставались коду из форков.

## Как перепроверить права

Выполняйте в SQL Editor Supabase. Первые два запроса только читают каталог.

```sql
-- RLS и политики
select c.relname, c.relrowsecurity,
       (select string_agg(policyname || ' [' || cmd || ' to ' || array_to_string(roles, ',') || ']', '; ')
        from pg_policies p where p.schemaname = 'public' and p.tablename = c.relname) as policies
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r' order by 1;

-- Колонки, доступные анониму на чтение
select table_name, string_agg(column_name, ', ' order by column_name)
from information_schema.column_privileges
where table_schema = 'public' and grantee = 'anon' and privilege_type = 'SELECT'
group by 1 order by 1;
```

Проверка «изнутри» (блок выбрасывает исключение в конце, поэтому ничего не сохраняется): переключиться на `set local role anon`
и попытаться прочитать `articles.body`, `claims.embedding`, `pipeline_runs` и вставить строку в `sources`.

Результат проверки 2026-11-18 от имени `anon`:

| Действие | Результат |
|---|---|
| `select articles.body` | отказано ✓ |
| `select claims.embedding` | отказано ✓ |
| `select * from pipeline_runs` | отказано ✓ |
| `select * from monitoring_checks` | без ошибки, строк нет (RLS) ✓ |
| `insert into sources` | отклонено политикой RLS ✓ |

## Закрыто миграциями 2026-11-18

Применены к живой базе, файлы: [20261117191848_restrict_anon_writes_and_centroids.sql](../supabase/migrations/20261117191848_restrict_anon_writes_and_centroids.sql)
и [20261117192143_revoke_anon_maintain.sql](../supabase/migrations/20261117192143_revoke_anon_maintain.sql).

- **Запись закрыта одним слоем.** Раньше `anon` и `authenticated` имели `INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER`
  (настройки Supabase по умолчанию), и записи не было только из-за отсутствия write-политик RLS. Теперь эти гранты отозваны на
  всех таблицах `public`, в том числе `MAINTAIN` (отдельный грант Postgres 17, его пришлось закрывать второй миграцией).
  Права по умолчанию для таблиц, которые создаёт роль `postgres`, тоже изменены: новые таблицы получают для `anon` и
  `authenticated` только `SELECT`.
- **Векторы `centroid`** в `events`, `claim_groups`, `stories` больше не читаются анонимом: фронт их не использует.
- **Служебные `gray_zone_verdicts`, `monitoring_checks`:** гранты `anon` отозваны, как у `pipeline_runs`.

Проверено: миграция сначала прогнана в откатываемой транзакции, затем применена; в самих миграциях есть самопроверка, которая
откатывает их при несовпадении. После применения 0 привилегий кроме `SELECT` у `anon` и `authenticated`, `service_role` и
`admin_reader` сохранили доступ, воркерные тесты на БД проходят (25 из 25), страницы `/`, `/events/[id]`, `/events/[id]/compare`,
`/stories/[id]` открываются без ошибок.

Что изменилось в работе: **новые колонки** таблиц `articles`, `claims`, `events`, `claim_groups`, `stories` анону нужно выдавать
явно миграцией (`grant select (...)`), иначе фронт не сможет их прочитать.

Остаточная оговорка: права по умолчанию для таблиц, созданных ролью `supabase_admin` (например, из панели Supabase), не менялись:
они принадлежат другой роли. Создавайте таблицы миграциями, тогда это не затрагивает.

## Открытые пробелы

Ничего из этого не исправлено: решение за вами.

1. **Воркер работает от `postgres`.** Утечка `DATABASE_URL` из GitHub Secrets или `worker/.env` даёт полный доступ.
   Отдельная роль воркера с правами только на нужные таблицы снизила бы ущерб. Приоритет низкий для проекта в одиночной разработке.
2. **Одна общая учётная запись админа.** Нет отдельных логинов и журнала входов. Отозвать выданную сессию можно только сменой
   `ADMIN_SESSION_SECRET` (или пароля, если секрет не задан). Счётчик неудачных входов хранится в памяти процесса и на serverless
   не общий между экземплярами (это отмечено в коде): основная защита — длина пароля.
3. **Подключение `admin_reader` без проверки сертификата** (`rejectUnauthorized: false`: сертификат пулера Supabase не в
   хранилище Node.js). Канал зашифрован, но имя сервера не проверяется. Для read-only роли это приемлемо; лучше подключить CA Supabase.
4. **Не проверял:** включена ли регистрация в Supabase Auth. Если да, любой может получить роль `authenticated`. Повышения прав это
   не даст (права как у `anon`), но проверить настройку стоит.

## Если появятся пользователи (этап 6)

Подписки на сущности и уведомления потребуют аккаунтов. Тогда роль `authenticated` получит собственные таблицы (подписки)
с политиками `user_id = auth.uid()`, а по умолчанию новые таблицы теперь выдают `authenticated` только `SELECT`: права на запись своим таблицам нужно будет
выдавать явно. Добавьте в эту матрицу столбец «Пользователь».
