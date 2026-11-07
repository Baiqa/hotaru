# Этап 2, подзадача 2 — Схлопывание фактов в три корзины

Per CLAUDE.md §5 Этап 2, второй пункт: "схлопывание фактов, три корзины (подтверждено /
уникально / спорно)". Работает на уровне события (claims всех статей-участников события),
не на уровне одной статьи — впервые в проекте сопоставляем claims МЕЖДУ статьями.

## Решено с пользователем

- "Источник" для подсчёта подтверждений = **издание** (source), не статья. Три статьи
  одного РИА про один факт = 1 источник, не 3.
- Механизм сопоставления: **эмбеддинги claims + порог, LLM только для серой зоны** (по
  аналогии с gray_zone.py для событий), не чистый LLM на всё событие целиком.
- Результат **хранится в БД** (новые таблицы `claim_groups`/`claim_group_members`), не
  пересчитывается на лету при каждой генерации.

## Схема (новая миграция)

```sql
-- embedding на claims — упомянут в §4 CLAUDE.md, был отложен в миграции claims до
-- реального потребителя (этот самый шаг).
alter table claims add column embedding vector(768);
create index claims_embedding_hnsw_idx on claims using hnsw (embedding vector_cosine_ops);

create table claim_groups (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references events(id) on delete cascade,
  bucket text not null check (bucket in ('confirmed', 'unique', 'disputed')),
  source_count int not null default 0,
  centroid vector(768),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index claim_groups_event_id_idx on claim_groups (event_id);

-- claim_id — PK: один claim принадлежит ровно одной группе (внутри своего события).
-- relation='contradicts' — этот claim ПРОТИВОРЕЧИТ другим участникам группы (а не просто
-- подтверждает) — по этому полю считается bucket='disputed'.
create table claim_group_members (
  claim_id uuid primary key references claims(id) on delete cascade,
  claim_group_id uuid not null references claim_groups(id) on delete cascade,
  relation text not null default 'confirms' check (relation in ('confirms', 'contradicts')),
  similarity_score double precision,
  joined_at timestamptz not null default now()
);
create index claim_group_members_group_id_idx on claim_group_members (claim_group_id);

alter table claim_groups enable row level security;
alter table claim_group_members enable row level security;
create policy "claim_groups are publicly readable" on claim_groups for select to anon, authenticated using (true);
create policy "claim_group_members are publicly readable" on claim_group_members for select to anon, authenticated using (true);
```

Кеш "обработан ли claim" = "есть ли у claim.id строка в claim_group_members" — по тому же
принципу, что entities_extracted_at/claims_extracted_at, но здесь это сама связь, без
отдельной timestamp-колонки на claims (аналогично events.centroid как естественный
маркер, а не доп. флаг).

## Алгоритм (`claim_grouping.py`)

Инкрементально, на уровне одной статьи/события (не батчем по всему корпусу):

1. Взять claims статьи без записи в `claim_group_members`.
2. Для события этой статьи — подтянуть существующие `claim_groups` (с centroid).
3. Для каждого нового claim:
   a. Посчитать косинус-близость к centroid каждой существующей группы события.
   b. Взять топ-3 кандидата (как в Этапе 1 `CANDIDATE_TRIES=3` — не только топ-1, это
      защита от той же ошибки с ложным разделением, что калибровали в Этапе 1).
   c. Для каждого кандидата по порядку:
      - `similarity < LOW_THRESHOLD (0.55)` → не кандидат, пропустить.
      - `similarity >= HIGH_THRESHOLD (0.80)` **и** числа (`claim_numbers`) не
        конфликтуют (нет пары с одинаковым `label`/`period`, но разным `value`) →
        `relation='confirms'`, присоединить, остановиться.
      - иначе (между порогами, или высокая близость с конфликтом чисел) → LLM
        (`claim_relation_classify.txt`): `confirms | contradicts | different`.
        - `confirms` → присоединить как `confirms`, остановиться.
        - `contradicts` → присоединить как `contradicts` (группа станет `disputed`),
          остановиться.
        - `different` → следующий кандидат.
   d. Если ни один кандидат не подошёл — новая группа (`bucket='unique'` пока
      source_count=1), claim — единственный член с `relation='confirms'`.
4. После присоединения — пересчитать у затронутой группы: `centroid` (среднее
   эмбеддингов членов), `source_count` (distinct `sources.id` среди статей членов),
   `bucket`:
   - есть хоть один член с `relation='contradicts'` → `disputed`;
   - иначе `source_count >= 2` → `confirmed`;
   - иначе → `unique`.

LLM-кеш по паре claim_id (сортированный, как `gray_zone_verdicts`) — чтобы не дублировать
вызовы, если тот же claim сравнивается в будущем запуске (claim уже обработан не будет,
но на случай пересчёта/дебага кеш не помешает). Решение: **не делаем отдельный кеш в этой
подзадаче** — claim обрабатывается один раз за свою жизнь (как claims_extracted_at), так
что кеш по паре не нужен (в отличие от gray_zone, где одна и та же пара статей может
встретиться с разных сторон по многу раз при слиянии/разделении событий).

## Новые файлы

- Миграция `supabase/migrations/<ts>_claim_groups.sql` (выше).
- `prompts/claim_relation_classify.txt` — LLM-промпт: даны текст+evidence двух claims,
  вернуть `confirms | contradicts | different`.
- `embed_claims.py` — отдельный шаг (по образцу `embed_articles.py`, переиспользует
  `embeddings.embed_text()`): эмбеддинг `claims.text` (не evidence — то же текстовое
  поле, что сравниваем). Entry point `hotaru-embed-claims`.
- `claim_grouping.py` — алгоритм выше (чистая логика + LLM-вызов, без раннера).
- `group_claims.py` — раннер (батч, по образцу `extract_entities.py`: новое соединение на
  батч, retry на `OperationalError`). Entry point `hotaru-group-claims`.
- `db.py`: `claims_missing_embedding`, `update_claim_embedding`,
  `claims_missing_group` (join claims→articles→event_articles, где claim.id нет в
  claim_group_members, событие активно), `event_claim_groups_with_centroid`,
  `create_claim_group`, `add_claim_group_member`, `recompute_claim_group` (centroid +
  source_count + bucket).

## Известное ограничение (документируем, не чиним сейчас)

Если событие A (у которого уже есть claim_groups) позже СЛИВАЕТСЯ в событие B через
`event_merge.py` — `event_articles` переезжают на B, но `claim_groups.event_id` остаются
указывать на A (осиротевшие). `event_merge.py` в этой подзадаче не трогаем — отдельное
решение на будущее (как Минфин/Habr-кейсы в Этапе 1).

## Критерий готовности подзадачи

- Прогнали на реальных событиях с 2+ статьями (из текущих ~480 активных событий).
- Хотя бы одно событие с `bucket='confirmed'` и source_count>=2 — вручную проверено, что
  факт действительно подтверждён разными изданиями.
- Хотя бы один реальный (не искусственный) случай `bucket='disputed'` — вручную проверено,
  что LLM-классификация на `contradicts` оправдана (реальное расхождение в деталях/числах).
- Повторный прогон `hotaru-group-claims` не создаёт дублей (claim, уже в
  claim_group_members, не трогается).
- Группа с одним членом (`source_count=1`) → `bucket='unique'`, не `confirmed`.
