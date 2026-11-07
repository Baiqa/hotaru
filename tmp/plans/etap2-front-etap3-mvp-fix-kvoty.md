# Этап-2 фронт + Этап-3 MVP (World Bank) + фикс фоновых джоб

## Контекст

Этап 2 (факты и синтез) формально не готов: код написан и протестирован, но
покрытие тонкое (712/4734 claims сгруппировано, 48 renders на 28 событий из 930)
и фронт вообще не показывает синтезированный текст — страница события до сих пор
рисует только список статей-источников. Причина тонкого покрытия — оба фоновых
джоба (`group_claims`, `synthesize`) делят один лимит Gemini free-tier (500
generate_content/день) и были не плановыми cron-прогонами, а ad-hoc background
shell'ами, которые молча умерли при закрытии прошлой сессии.

При проверке реальных `claim_numbers` (а не по спецификации) выяснилось, что
подавляющее большинство чисел в корпусе — не экономические (день месяца, возраст,
весовая категория, погибшие, версия ПО) и графика им не нужно вообще. Поэтому
Этап 3 в этом раунде — не полная реализация (4 источника, 3 типа графика), а один
вертикальный срез: World Bank + линия + куцый allowlist по `label`/`unit`
(инфляция, % → Казахстан + Россия).

Отдельно — найден реальный баг, не просто "медленно": при полностью исчерпанной
дневной квоте ни `group_claims.py`, ни `synthesize.py` не помечают пропущенный
элемент как обработанный, поэтому внешний `while True` в обоих файлах зацикливается
**бесконечно** на одном и том же батче (подтверждено чтением обоих файлов). Это
прямая причина жалобы "через несколько часов замечаем, что ничего не делалось" —
без фикса cron-расписание с разнесением по времени не даёт реальной гарантии.

Три части ниже не зависят друг от друга по коду, но часть 3 (фикс квоты) должна
быть сделана **до** постановки cron-расписания, иначе разнесение по времени
бессмысленно.

## Часть 1 — Этап-2 фронт: синтезированный текст на странице события

**`web/src/lib/events.ts`** — экспортировать `resolveActiveEventId` (сейчас
приватная функция на строке 131); новый код обязан проходить через неё же для
merged-событий.

**Новый `web/src/lib/renders.ts`** — отдельный файл (свой домен: renders/
render_claims/claims/claim_numbers/claim_groups), копирует конвенции `events.ts`/
`articles.ts` (свой `UUID_RE`, свой `one<T>()`, `maybeSingle()`).

`getEventRender(id, format)`:
1. `UUID_RE` guard → `resolveActiveEventId(id)`.
2. `renders` по `event_id`+`format`+`lang='ru'` через `maybeSingle()` — нет строки
   → `null` (нормальный случай при тонком покрытии, НЕ ошибка).
3. `render_claims` по `render_id`, embed `claims(evidence, articles(canonical_url,
   sources(name)), claim_group_members(claim_groups(bucket, source_count)),
   claim_numbers(value, unit, label))`.
4. Собрать `body.split("\n")` (1:1 с `sentence_index` по контракту схемы) +
   сматченные claims в `{ sentences: [{ text, claims: [...] }] }`.

Бейдж `claim_groups.bucket` — включаем сразу (нулевая доп. стоимость, уже в том
же запросе), но: если `claim_group_members` нет для claim — `bucket: null` и бейдж
**не рисуется** (не дефолтим в `'unique'`, как делает `event_facts_for_synthesis`
для синтеза — там это осознанный дефолт "фактически не подтверждён", на фронте
это было бы ложным "только один источник" для claim, который просто ещё не
проверяли при 15% покрытии).

**`web/src/app/events/[id]/page.tsx`** — между мета-строкой и текущим списком
статей: `getEventRender(id, "summary_30s")`, затем список предложений, каждое в
`<details>/<summary>` (нативный disclosure, zero JS — первая интерактивность в
кодовой базе, как и решили). Открытое `<details>` показывает evidence, источник
(ссылка на `canonical_url`), бейдж confirmed/disputed/unique (disputed —
единственное исключение из чисто-zinc палитры, amber, т.к. это ключевой
дифференциатор продукта). Нет рендера → `<p>` "Краткая сводка пока не готова"
вместо ошибки. Текущий список статей получает заголовок "Источники" (теперь на
странице два смысловых блока).

## Часть 2 — Этап-3 MVP: числа → графики (World Bank, линия)

**Новая миграция `supabase/migrations/20261105100000_metrics.sql`**:
```sql
create table metric_series (
  id uuid primary key default gen_random_uuid(),
  key text not null unique,        -- 'FP.CPI.TOTL.ZG:KAZ'
  label text not null,
  unit text not null,
  source_name text not null,
  source_url text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create trigger metric_series_set_updated_at before update on metric_series
  for each row execute function set_updated_at();

create table metric_points (
  id uuid primary key default gen_random_uuid(),
  series_id uuid not null references metric_series(id) on delete cascade,
  period text not null,            -- год как строка, как claim_numbers.period
  value numeric not null,
  unique (series_id, period)
);
create index metric_points_series_id_idx on metric_points (series_id);

alter table metric_series enable row level security;
alter table metric_points enable row level security;
create policy "metric_series are publicly readable" on metric_series
  for select to anon, authenticated using (true);
create policy "metric_points are publicly readable" on metric_points
  for select to anon, authenticated using (true);
```

**Воркер `worker/src/hotaru_worker/metrics.py`** (новый, без `genai` — не
зависит от Gemini-квоты, собственное расписание): хардкод-список `SERIES` = 2
записи (`FP.CPI.TOTL.ZG` × KAZ/RUS), `fetch_series()` зовёт
`api.worldbank.org/v2/country/{iso3}/indicator/{code}?format=json` (без ключа),
`run_once()` апсертит через новые `db.upsert_metric_series`/`db.upsert_metric_points`.
Entry point `hotaru-fetch-metrics` в `worker/pyproject.toml`.

**Матчинг claim_numbers → metric_series — на фронте, не на воркере.** В этом
MVP страна всегда одна из двух хардкод-значений (Казахстан/Россия) — нет ничего
claim-специфичного, что стоило бы считать заранее отдельной джобой/таблицей-линком.
`web/src/lib/metrics.ts`: `ALLOWLIST` = `[{ indicatorCode: "FP.CPI.TOTL.ZG",
countries: [KAZ, RUS], test: (label, unit) => /инфляц/i.test(label) && unit === "%" }]`,
`matchMetric(label, unit)`, `getMetricChartData(rule)` (запрос `metric_series` по
`key in (...)` + embed `metric_points`).

Правило §3.5 "график только по числу с evidence из текста" выполняется
конструктивно: `claim_numbers` в `renders.ts` приходят только через
`render_claims → claims` этого события, `evidence` у `claims` всегда `not null`
по constraint'у — отдельной проверки не нужно, только комментарий в коде.

**`web/src/components/MetricLineChart.tsx`** (новый, серверный компонент, чистый
inline SVG — ~15 годовых точек на 2 страны, ручная min/max/scale арифметика проще,
чем новая зависимость). Палитра: zinc-700 + amber-700 (две линии), подпись оси,
unit + источник со ссылкой.

Точка вызова: внутри того же `<details>` предложения, что и evidence/источник
(часть 1) — то есть "клик по числу → источник" реализуется на уровне предложения
целиком, не отдельной точки графика (точечный hover/tooltip требует client JS —
осознанно вне MVP, первое исключение из "zero client components" лучше делать
отдельным обдуманным раундом). Данные графика грузятся один раз на уровне
страницы (не из вложенного цикла) в `Map` по `indicatorCode`.

## Часть 3 — Фоновые джобы: фикс бесконечного цикла + cron

**Подтверждённый баг** (прочитан код обоих файлов): `group_claims.py` строки
28-55 и `synthesize.py` — внешний `while True` выходит только когда
`claims_missing_group`/`events_missing_render` возвращают пусто. При 429 элемент
не помечается обработанным (`continue`/`skipped += 1` без записи в БД) — значит
тот же батч вернётся на следующей итерации **того же `while True`** и так
бесконечно, если квота исчерпана на весь день. Это не "медленно", это зависание
навсегда до ручной остановки.

**Фикс — новый `worker/src/hotaru_worker/quota.py`**: `has_quota(client, model)`
делает один минимальный `generate_content` вызов без retry; `APIError.code == 429`
→ `False` (квота кончилась, не входим в батч-цикл), любая другая ошибка
пробрасывается как раньше (не маскируем реальные сбои под "квота").

В `group_claims.py` и `synthesize.py` — вызов `quota.has_quota(...)` сразу после
создания `client`, **до** `while True`; при `False` — лог + немедленный возврат
со статистикой `skipped_quota_exhausted: True`.

**Cron-расписание** (создаётся через `CronCreate` отдельным шагом **после**
фикса — порядок важен):

| Джоба | Расписание | Команда (cwd `worker/`) |
|---|---|---|
| `group_claims` | `0,30 * * * *` | `uv run --no-sync python -m hotaru_worker.group_claims` |
| `synthesize` | `15,45 * * * *` | `uv run --no-sync python -m hotaru_worker.synthesize` |
| `metrics` | `0 3 * * *` (раз в сутки) | `uv run --no-sync python -m hotaru_worker.metrics` |

`group_claims`/`synthesize` разнесены на 15 минут — делят одну квоту, не должны
идти одновременно (по памяти проекта). `metrics` не трогает Gemini, отдельное
суточное расписание.

## Файлы

Новые: `web/src/lib/renders.ts`, `web/src/lib/metrics.ts`,
`web/src/components/MetricLineChart.tsx`, `supabase/migrations/20261105100000_metrics.sql`,
`worker/src/hotaru_worker/metrics.py`, `worker/src/hotaru_worker/quota.py`.

Изменённые: `web/src/lib/events.ts` (export), `web/src/app/events/[id]/page.tsx`,
`worker/src/hotaru_worker/db.py` (+`upsert_metric_series`/`upsert_metric_points`),
`worker/src/hotaru_worker/group_claims.py`, `worker/src/hotaru_worker/synthesize.py`
(+quota-check), `worker/pyproject.toml` (+`hotaru-fetch-metrics`).

## Проверка

- `cd web && npm run build && npm run lint` — чисто.
- Открыть `/events/[id]` для события из 28 с готовым рендером — видно текст,
  клик по предложению раскрывает evidence+источник(+бейдж); для события без
  рендера — "пока не готова", без ошибки.
- `uv run --no-sync python -m hotaru_worker.metrics` вручную один раз — проверить
  `select * from metric_series`, `metric_points` заполнились; на странице события
  с claim "инфляция ... %" — виден график с двумя линиями, ссылка на World Bank.
- Вручную проверить фикс квоты: смок-тест при реально исчерпанной квоте возвращает
  `False` до входа в `while True` (лог "дневная квота похоже исчерпана"), прогон
  завершается за секунды, а не зависает/не крутит батч бесконечно.
- После фикса — поставить cron через `CronCreate` по таблице выше, затем раз
  проверить `CronList`, что все 3 задачи созданы с правильным расписанием/командой.
