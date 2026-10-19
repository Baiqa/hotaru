# Этап 2, подзадача 1 — Извлечение фактов с evidence, кеш по хешу

Per CLAUDE.md §7: одна подзадача этапа, не весь Этап 2 целиком. Это первый пункт из §5
Этап 2: "Извлечение фактов с evidence, кеш по хешу." Остальные пункты (схлопывание фактов
в три корзины, генерация сводки/разбора, пост-проверка, рендеры) — следующие подзадачи.

## Решено с пользователем

- Модель: `gemini-3.1-flash-lite-preview` — та же, что уже используется в `gray_zone.py`
  (retry/backoff инфраструктура уже отлажена под неё).
- Связь claim ↔ entities (Этап 1) — НЕ делаем сейчас. Схема `claims` по §4 CLAUDE.md не
  содержит такого поля; откладываем до реального потребителя (схлопывание фактов).
- Проверка evidence — строгая подстрока после нормализации пробелов (не fuzzy): собрать
  `" ".join(text.split())` для evidence и для title+lead+body, проверить `in`.

## Схема (новая миграция)

```sql
create table claims (
  id uuid primary key default gen_random_uuid(),
  article_id uuid not null references articles(id) on delete cascade,
  text text not null,
  type text not null check (type in ('fact', 'statement', 'estimate')),
  evidence text not null,
  created_at timestamptz not null default now()
);
create index claims_article_id_idx on claims (article_id);

create table claim_numbers (
  id uuid primary key default gen_random_uuid(),
  claim_id uuid not null references claims(id) on delete cascade,
  value numeric not null,
  unit text,
  label text,
  period text
);
create index claim_numbers_claim_id_idx on claim_numbers (claim_id);

-- кеш по хешу (раздел 3.2 CLAUDE.md): content_hash уже unique на articles, так что
-- "обработана ли эта статья" == "проставлен ли claims_extracted_at" на этой строке —
-- по образцу entities_extracted_at, второй такой же статьи просто не может быть вставлено.
alter table articles add column claims_extracted_at timestamptz;

alter table claims enable row level security;
alter table claim_numbers enable row level security;
create policy "claims are publicly readable" on claims for select to anon, authenticated using (true);
create policy "claim_numbers are publicly readable" on claim_numbers for select to anon, authenticated using (true);
```

`embedding vector` на `claims` (упомянут в §4) — НЕ добавляется этой миграцией, по тому
же принципу, что `articles.embedding` добавляли отдельной миграцией в Этапе 1: нужен он
только для "схлопывания фактов", не для самой экстракции.

## Промпт `prompts/extract_claims.txt`

Вход: title + lead + body статьи (полный текст — внутренняя обработка, раздел 3.1
CLAUDE.md это явно разрешает). Инструкция моделe:
- Разбить статью на дискретные фактические утверждения.
- `type`: `fact` (подтверждённое событие/действие), `statement` (кто-то что-то заявил/
  сказал), `estimate` (прогноз/оценка/расчёт).
- `evidence` — точная цитата из текста, без парафраза (жёсткое правило: не найдём —
  выбросим).
- `numbers`: `value`, `unit`, `label`, `period` — только числа, относящиеся к claim.
- Структурированный JSON-ответ через `response_schema` (Pydantic), как в `gray_zone.py`.

## Код

- `claim_extraction.py` (новый, логика — по образцу `entities.py`/`gray_zone.py`):
  Pydantic-схема `Claim`/`ClaimNumber`/`ClaimsResponse`, функция
  `extract_claims(client, article) -> list[dict]` с тем же retry/backoff, что в
  `gray_zone.classify()`, плюс фильтрация по evidence-проверке внутри.
- `extract_claims.py` (новый, раннер — по образцу `extract_entities.py`): батч=50, новое
  соединение на батч, retry при `OperationalError`, `db.articles_missing_claims`,
  `db.insert_claims`, `db.mark_claims_extracted` (ставится даже если выживших claims 0 —
  кеш не должен гонять модель по статье повторно, даже если в прошлый раз ничего не
  извлеклось).
- `db.py`: три новые функции (`articles_missing_claims`, `insert_claims`,
  `mark_claims_extracted`).
- `pyproject.toml`: `hotaru-extract-claims = "hotaru_worker.extract_claims:main"`.

## Критерий готовности этой подзадачи

- Прогнали на реальном корпусе (548 статей).
- Ни один сохранённый `claim.evidence` не является перефразом — только точные подстроки
  исходного текста (после нормализации пробелов).
- Повторный прогон `hotaru-extract-claims` не трогает уже обработанные статьи
  (`claims_extracted_at` работает как кеш, не создаёт дублей).
- Числа в `claim_numbers` согласованы 1:1 с тем, что модель вернула внутри каждого claim.
