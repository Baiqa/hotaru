# План: Этап 1 → «Сущности: извлечение, таблица алиасов, нормализация»

Дата: 2026-09-25. Подзадача — первый подпункт Этапа 1 (CLAUDE.md §5), не весь этап целиком.
Решения по клариф. вопросам этой сессии: NER — spaCy/NER библиотека; LLM/embeddings провайдер —
OpenRouter (ключ сохранён в worker/.env, в этой подзадаче не используется — понадобится для
эмбеддингов/серой зоны кластеризации, отдельной подзадачей); B-Cubed оценка — строим, но только
когда появятся events (не в этой подзадаче).

## 1. Миграция БД (supabase/migrations/)
- entities (id, canonical_name, type person|org|place, external_id, created_at, updated_at)
  — unique (canonical_name, type)
- entity_aliases (id, entity_id→entities, alias, lang ru|kk|en, created_at)
  — unique index на (lower(alias), lang)
- article_entities (article_id→articles, entity_id→entities, mention_count)
  — PK (article_id, entity_id) + индекс на entity_id
- articles.entities_extracted_at timestamptz — новая колонка (по образцу sources.robots_checked_at)
- RLS: публичный SELECT на все три новые таблицы, как у sources/articles

## 2. Зависимости воркера
- spacy + ru_core_news_sm, en_core_web_sm (kk-модели у spaCy нет, kk-источников тоже пока нет)
- pymorphy3 + pymorphy3-dicts-ru — лемматизация ru-сущностей к именительному падежу
- rapidfuzz — нечёткое сопоставление алиасов

## 3. Модуль worker/src/hotaru_worker/entities.py
- извлечение упоминаний нужной spaCy-моделью по языку статьи
- маппинг меток spaCy → person|org|place (остальное отбрасывается)
- нормализация имени (в normalize.py): ru — лемматизация через pymorphy3, en — trim/схлопывание пробелов
- резолюция: точный алиас → нечёткое сравнение (rapidfuzz, порог ~92) → новая сущность
- article_entities.mention_count, простановка entities_extracted_at

## 4. Сид алиасов (aliases_seed.yaml, по образцу sources.yaml)
Несколько показательных случаев для старта, включая пример из CLAUDE.md
(Нацбанк / НБ РК / Национальный банк Казахстана).

## 5. Точка входа hotaru-extract-entities
Отдельный проход (модель грузится раз на батч). В scheduler.py пока не подключаю.

## 6. Проверка перед «готово»
- прогон на всех текущих статьях (~548+) без падений
- ручная проверка схлопывания алиасов на реальных данных
- идемпотентность повторного прогона (без дублей алиасов, без повторной обработки)
- рост mention_count при повторных упоминаниях в одной статье
- B-Cubed разметку НЕ делаем здесь — она про склейку events, вернёмся в подзадаче кластеризации
