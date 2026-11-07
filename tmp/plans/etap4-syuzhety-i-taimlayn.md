# Этап 4 (без LLM-части) — сюжеты и таймлайн

## Контекст

Этап 4 — следующая стадия по CLAUDE.md §5: привязка событий к сюжету (широкое
окно, мягкий порог) + таймлайн. Пользователь явно решил делать эту стадию
**без** LLM-классификации `event_links` (continuation/reaction/refutation/
consequence) — она требует семантической оценки связи между событиями, то есть
ещё одной нагрузки на дефицитную квоту Gemini, которая уже душит group_claims/
synthesize. Привязка событие→сюжет сама по себе квоты не требует: CLAUDE.md
§3.3 описывает её как мягкий порог по центроиду, тот же принцип, что уже
работает в `event_merge.py` для склейки событий — просто с другим (широким)
окном и другим (низким) порогом.

«Что нового с прошлого раза» (4-й пункт Этапа 4) тоже отложен — ему нужен
механизм "последний визит" (localStorage/cookie), которого в полностью
анонимном, целиком server-rendered приложении сейчас нет; это отдельная
осмысленная подзадача, не часть этого раунда (тот же принцип "один раздел
этапа за раз", CLAUDE.md §7).

Таймлайн — **вертикальный список, не горизонтальная схема метро**: единственное
жёсткое требование §5 — «читается на телефоне без горизонтального скролла»,
а вертикальный список тривиально удовлетворяет этому при любом числе событий,
в отличие от горизонтальной метро-схемы, которую на 20+ остановок уложить в
375px без скролла принципиально сложно. Эстетика метро — не требование,
читаемость на телефоне — требование.

## Данные/алгоритм — зеркалирует `event_merge.py`

`worker/src/hotaru_worker/event_merge.py` — ближайший существующий аналог:
центроид-косинус + общая сущность, `db.find_similar_events` (shared-entity
EXISTS + окно по времени + pgvector top-N), один `run_once(conn)` на всё
множество активных событий (не батч-реконнект — рабочее множество ограничено,
тот же паттерн уже используется для merge/split событий). Новый
`worker/src/hotaru_worker/story_assignment.py` копирует эту структуру один
уровень выше: событие→сюжет вместо статья→событие.

**Важное отличие от merge событий:** `event_merge` требует ОБА сигнала —
косинус ≥0.85 И majority-entity overlap (иначе склеивает Habr-мусор по общей
NER-шумовой "сущности"). Для сюжета majority-overlap неверен по конструкции:
сюжет — это «решение → реакция → последствие», сущности естественно дрейфуют
между первым и пятым событием одной цепочки. Поэтому для сюжета — **только
мягкий косинус-порог + хотя бы одна общая сущность** (та же "хотя бы одна",
что уже используется на этапе отбора кандидатов), без majority-overlap.

```python
STORY_THRESHOLD = 0.55  # мягкий, неоткалиброванный стартовый порог — нет
                        # размеченных данных на уровне сюжетов вообще, как и
                        # исходные пороги Этапа 1 до калибровки
STORY_WINDOW_DAYS = 60  # "недели-месяцы" по CLAUDE.md §3.3, явная оценка
CANDIDATE_LIMIT = 5
```

Алгоритм `run_once(conn)`:
1. `db.events_missing_story(conn)` — активные события с `centroid is not null`,
   без строки в `story_events`.
2. Для каждого (по возрастанию `started_at`): `db.event_entity_ids` (уже
   существует, реюз) → `db.find_similar_stories` (новая функция, зеркало
   `find_similar_events`, но `stories`/`story_events` вместо `events`/
   `event_articles`) → если топ-кандидат `cosine_sim >= STORY_THRESHOLD` —
   присоединить (`db.assign_event_to_story`); иначе — `db.create_story` (новый
   сюжет, `title`/`started_at`/`centroid` от этого события, та же заглушка-
   логика, что `events.title` у событий).
3. При присоединении — полный, не инкрементальный, пересчёт centroid сюжета
   (`recompute_story_centroid`, по образцу `recompute_event_centroid`) и
   пересчёт `position` всех членов по `events.started_at` (`recompute_story_
   positions`) — гарантирует хронологический порядок независимо от порядка
   обработки в конкретном прогоне.

**Первый прогон обработает все ~930 активных событий и создаст много
одноэлементных сюжетов** (большинство событий ни с кем не успеют совпасть на
самом первом проходе) — тот же бутстрэп-паттерн, что был у событий до
`event_merge`. Слияния/разделения сюжетов (аналог merge/split событий) CLAUDE.md
для Этапа 4 не требует — не строим в этом раунде.

## Миграция `supabase/migrations/20261105<HHMMSS>_stories.sql`

```sql
create table stories (
  id uuid primary key default gen_random_uuid(),
  title text not null,       -- заглушка: title события-основателя, как у events.title
  started_at timestamptz not null,
  updated_at timestamptz not null default now(),
  centroid vector(768),
  status text not null default 'active' check (status in ('active', 'merged')),
  created_at timestamptz not null default now()
);
-- 'merged' зарезервирован на будущее (по аналогии с events) — merge-джобы для
-- сюжетов в этом раунде нет, никто пока не пишет это значение.

create table story_events (
  event_id uuid primary key references events(id) on delete cascade,
  story_id uuid not null references stories(id) on delete cascade,
  position int not null,
  joined_at timestamptz not null default now()
);
create index story_events_story_id_idx on story_events (story_id);

alter table stories enable row level security;
alter table story_events enable row level security;
create policy "stories are publicly readable" on stories for select to anon, authenticated using (true);
create policy "story_events are publicly readable" on story_events for select to anon, authenticated using (true);
```

(CLAUDE.md §4 не упоминает `slug` как обязательное для URL — используем `id`
как везде в проекте, не добавляем `slug` без нужды.)

Новые функции в `worker/src/hotaru_worker/db.py`: `events_missing_story`,
`find_similar_stories`, `create_story`, `assign_event_to_story`,
`recompute_story_centroid`, `recompute_story_positions`.

Entry point `worker/pyproject.toml`: `hotaru-assign-stories =
"hotaru_worker.story_assignment:main"` (имя в стиле существующих
`hotaru-merge-events`/`hotaru-split-events`).

## Фронт

**Новый `web/src/lib/stories.ts`**: `getStoryById(id)` — `UUID_RE` guard,
`stories` по id (`status='active'`, иначе 404 — merge для сюжетов не строим,
canonical-цепочка не нужна), embed `story_events(position, events(id, title,
started_at))` отсортированный по `position`.

**Новый `web/src/app/stories/[id]/page.tsx`** + `not-found.tsx` (тот же
паттерн, что `events/[id]`): заголовок сюжета, вертикальный таймлайн —
каждое событие точкой на вертикальной линии (простой CSS: `border-left` +
кружок), дата + `events.title` + ссылка на `/events/[id]`, хронологически от
старого к новому (цепочка "решение → реакция → последствие" читается сверху
вниз, в отличие от общей ленты, которая новое-сверху).

**Изменение `web/src/app/events/[id]/page.tsx`**: если у события есть сюжет —
строка-ссылка `Сюжет: {story.title} →` на `/stories/[id]`, рядом с «Сравнить
подачи →». Если сюжета нет (событие не дошло до `hotaru-assign-stories`) —
ссылка просто не рисуется (не "недостаточно данных", как у рендера/сравнения —
здесь нет смысла линковать на то, чего гарантированно не существует). Нужна
`getStoryForEvent(eventId)` в `stories.ts`: `story_events` по `event_id` →
`story_id` → `stories.title`.

## Cron

После проверки вручную — добавить `hotaru-assign-stories` в расписание
(аналог `hotaru-merge-events`, не завязан на Gemini-квоту, можно чаще, чем
group_claims/synthesize): `CronCreate` раз в час со сдвигом от уже занятых
минут.

## Проверка

- Применить миграцию к живой БД, запустить `uv run --no-sync python -m
  hotaru_worker.story_assignment` вручную, проверить `select count(*) from
  stories`, `select count(*) from story_events` — не пусто, не ошибка.
- Найти в БД сюжет с 2+ событиями (по совместному центроиду/сущности — скорее
  всего такой найдётся среди уже известных "больших" событийных кластеров,
  например Иран/Ормузский пролив, если рядом есть связанные более поздние
  события) — убедиться, что `position` соответствует хронологии.
- `npm run build && npm run lint` в `web/` — чисто.
- В браузере: `/stories/[id]` для найденного многоэлементного сюжета —
  таймлайн рисуется, читается без горизонтального скролла (резайз на мобильную
  ширину); `/events/[id]` для события из этого сюжета показывает ссылку
  «Сюжет: ... →»; для события без сюжета — ссылки нет.
- Повторный запуск `hotaru-assign-stories` — идемпотентность (уже назначенные
  события не трогаются, `events_missing_story` их не возвращает).
