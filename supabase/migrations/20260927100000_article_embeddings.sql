-- Этап 1, подзадача "Эмбеддинги заголовка+лида + pgvector-индекс" (раздел 5 CLAUDE.md).
-- Скоринг/отбор кандидатов/events — следующие подзадачи, не эта миграция.

create extension if not exists vector;

-- 768 — один из рекомендуемых Google output_dimensionality для gemini-embedding-2
-- (128/768/1536/3072); модель сама нормализует усечённые векторы.
alter table articles add column embedding vector(768);

-- "Обработано" здесь ровно то же самое, что "embedding not null" — отдельного
-- флага вроде entities_extracted_at не заводим.
create index articles_embedding_hnsw_idx
  on articles using hnsw (embedding vector_cosine_ops);
