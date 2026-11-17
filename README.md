# Hotaru

Новостное приложение, которое не пересказывает статьи, а собирает **событие из нескольких источников**:
факты с атрибуцией, числа в историческом контексте, таймлайн сюжета, расхождения между изданиями.

Полная спецификация продукта, этапы и критерии готовности: [CLAUDE.MD](CLAUDE.MD).

## Как устроено

```
[Python-воркер]  →  [PostgreSQL + pgvector (Supabase)]  ←  [Next.js]
 сбор, извлечение,        хранение                          чтение, рендер
 кластеризация,
 генерация текстов
```

Фронт никогда не ходит в LLM и не парсит сайты: он только читает готовые данные из БД.

| Папка | Что внутри |
|---|---|
| `worker/` | Python-воркер (uv, Python 3.13): сбор RSS, сущности, эмбеддинги, события, факты, синтез, мониторинг |
| `web/` | Next.js 16 (App Router), TypeScript, Tailwind 4; PWA; закрытая страница `/admin` |
| `supabase/migrations/` | Схема БД. Меняется только миграциями |
| `docs/` | Документация: [матрица ролей и прав (RBAC)](docs/rbac.md) |
| `prompts/` | Промпты к моделям (отдельными файлами, не в коде) |
| `.github/workflows/` | Запуск воркера по расписанию (см. «Состояние запуска») |
| `tmp/plans/` | Рабочие планы задач; часть кода ссылается на них как на обоснование порогов |

## Требования

- [uv](https://docs.astral.sh/uv/) и Python ≥ 3.13 (воркер)
- Node.js ≥ 20.9 и npm (фронт)
- Проект Supabase (PostgreSQL с расширением `pgvector`)
- Ключ Google AI Studio (`GOOGLE_API_KEY`) для эмбеддингов и LLM; ключ Groq необязателен

## Быстрый старт

### 1. База данных

Примените файлы из [supabase/migrations/](supabase/migrations/) по порядку имён (Supabase CLI
`supabase db push` после `supabase link`, либо SQL Editor в панели проекта).

Миграция `20261117120000_admin_reader_role.sql` создаёт роль `admin_reader` без входа; пароль в git не
хранится, его задают отдельной командой (нужна только для `/admin`, см. ниже).

### 2. Воркер

```bash
cd worker
cp .env.example .env        # впишите DATABASE_URL, CONTACT_EMAIL, GOOGLE_API_KEY
uv sync                     # ставит зависимости и модели spaCy (ru, en)
uv run hotaru-collect       # первый сбор статей
```

### 3. Фронт

```bash
cd web
cp .env.example .env.local  # впишите NEXT_PUBLIC_SUPABASE_URL и NEXT_PUBLIC_SUPABASE_ANON_KEY
npm install
npm run dev                 # http://localhost:3000
```

Next.js здесь новее, чем в обучающих данных моделей: перед правками читайте
`web/node_modules/next/dist/docs/` (см. [web/AGENTS.md](web/AGENTS.md)).

## Переменные окружения

Файлы с настоящими значениями (`.env`, `.env.local`) в git не попадают. Шаблоны:
[worker/.env.example](worker/.env.example) и [web/.env.example](web/.env.example).

**Воркер (`worker/.env`):**

| Переменная | Обязательна | Назначение |
|---|---|---|
| `DATABASE_URL` | да | Строка подключения Supabase (Session pooler, порт 5432) |
| `CONTACT_EMAIL` | да | Контакт для честного User-Agent парсера (§3.1 спецификации) |
| `GOOGLE_API_KEY` | для эмбеддингов и LLM-шагов | Gemini API (читается клиентом `google-genai`) |
| `GROQ_API_KEY` | нет | Запасной провайдер, когда Gemini отвечает 429 по дневной квоте |
| `REQUEST_DELAY_SECONDS` | нет | Пауза между запросами к одному домену, по умолчанию 1.5 |
| `HTTP_TIMEOUT_SECONDS` | нет | Таймаут HTTP, по умолчанию 15 |
| `SOURCES_FILE`, `ALIASES_FILE` | нет | Свои `sources.yaml` / `aliases_seed.yaml` вместо встроенных |

**Фронт (`web/.env.local`, на хостинге — в настройках проекта):**

| Переменная | Обязательна | Назначение |
|---|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | да | URL проекта Supabase |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | да | Анонимный ключ; доступ ограничен RLS. `service_role` сюда не класть |
| `ADMIN_PASSWORD` | для `/admin` | Общий пароль, не короче 12 символов |
| `ADMIN_DATABASE_URL` | для `/admin` | Подключение ролью `admin_reader` (только SELECT) |
| `ADMIN_SESSION_SECRET` | нет | Секрет подписи сессии; без него используется `ADMIN_PASSWORD` |

Без `ADMIN_PASSWORD` и `ADMIN_DATABASE_URL` все адреса `/admin` отвечают 404.

Включение входа для роли (значения подставьте свои, команда выполняется вне репозитория):

```sql
alter role admin_reader with login password '<пароль>';
```

## Команды воркера

Запускаются из `worker/` как `uv run <команда>`. Порядок — как в конвейере
([pipeline.yml](.github/workflows/pipeline.yml)):

| Команда | Что делает |
|---|---|
| `hotaru-collect` | Сбор статей по RSS, полный текст через trafilatura, дедупликация |
| `hotaru-extract-entities` | Сущности и нормализация по таблице алиасов |
| `hotaru-embed-articles` | Эмбеддинги заголовка и лида |
| `hotaru-cluster-events` | Склейка статей в события (скоринг + LLM для серой зоны) |
| `hotaru-merge-events` / `hotaru-split-events` | Слияние и разделение событий |
| `hotaru-assign-stories` | Привязка событий к сюжетам |
| `hotaru-extract-claims` | Факты с `evidence` (`--scope multi` или `rest`) |
| `hotaru-embed-claims`, `hotaru-group-claims` | Эмбеддинги фактов, схлопывание дублей |
| `hotaru-synthesize` | Текст события со ссылками на факты |
| `hotaru-fetch-metrics` | Исторические ряды World Bank |
| `hotaru-monitor` | Проверки конвейера (§6 спецификации) |
| `hotaru-rebuild` | Пересборка производных данных: `plan`, `backup`, `delete`, `stories` |
| `hotaru-scheduler` | Цикл `collect` раз в 30 минут для локальной разработки |

Общие ключи: `--max-minutes N` (мягкий лимит времени шага), `--if-due-hours N` (запуск, только если
давно не запускался по журналу `pipeline_runs`).

## Тесты

```bash
cd worker
uv run pytest
```

Тесты с суффиксом `_db` ходят в БД из `DATABASE_URL` внутри транзакции, которая откатывается. Без
`DATABASE_URL` они пропускаются. Указывайте не рабочую базу, если не уверены, что откат вас устраивает.

Для фронта: `cd web && npm run lint && npm run build`.

## Состояние запуска

- Расписание GitHub Actions (`pipeline.yml`, каждые 30 минут) **отключено с 2026-11-17**: обработка
  накопленных данных идёт локально. Проверить текущее состояние: `gh workflow list`; включить:
  `gh workflow enable pipeline.yml`. Остальные четыре workflow только ручные.
- Секреты для Actions: `DATABASE_URL`, `CONTACT_EMAIL`, `GOOGLE_API_KEY`, `GROQ_API_KEY`.
- Хостинг фронта: Vercel (Root Directory = `web`). Хостинг воркера в спецификации остаётся открытым вопросом.
- Не запускайте `hotaru-synthesize` и `hotaru-group-claims` одновременно: они делят дневную квоту Gemini.
- Перед тем как считать данные свежими, смотрите `pipeline_runs` или страницу `/admin`.

## Правила работы

- Задачи ставятся по одному разделу этапа; у каждой есть проверяемый критерий готовности (§5, §7 спецификации).
- Схема БД меняется только миграциями.
- Полный текст чужих статей нужен только для внутренней обработки; пользователю показывается свой синтез,
  цитаты не длиннее одного предложения, всегда со ссылкой на оригинал.
