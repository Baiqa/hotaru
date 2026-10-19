import { supabase } from "./supabase";

export type Article = {
  id: string;
  title: string;
  lead: string | null;
  sourceName: string;
  sourceUrl: string;
  publishedAt: string | null;
};

type ArticleRow = {
  id: string;
  title: string;
  lead: string | null;
  canonical_url: string;
  published_at: string | null;
  sources: { name: string } | { name: string }[] | null;
};

const ARTICLE_SELECT = "id, title, lead, canonical_url, published_at, sources(name)";
const ARTICLE_LIST_LIMIT = 30;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function sourceName(row: ArticleRow): string {
  const s = row.sources;
  if (!s) return "Неизвестный источник";
  return Array.isArray(s) ? (s[0]?.name ?? "Неизвестный источник") : s.name;
}

function toArticle(row: ArticleRow): Article {
  return {
    id: row.id,
    title: row.title,
    lead: row.lead,
    sourceName: sourceName(row),
    sourceUrl: row.canonical_url,
    publishedAt: row.published_at,
  };
}

export async function listArticles(): Promise<Article[]> {
  const { data, error } = await supabase
    .from("articles")
    .select(ARTICLE_SELECT)
    .order("published_at", { ascending: false, nullsFirst: false })
    .limit(ARTICLE_LIST_LIMIT);

  if (error) {
    throw new Error(`Не удалось загрузить список статей: ${error.message}`);
  }
  return (data as unknown as ArticleRow[]).map(toArticle);
}

export async function getArticleById(id: string): Promise<Article | null> {
  // Невалидный UUID -> считаем "не найдено", а не даём Postgres упасть
  // с ошибкой синтаксиса и не показываем пользователю техническую ошибку.
  if (!UUID_RE.test(id)) {
    return null;
  }

  const { data, error } = await supabase
    .from("articles")
    .select(ARTICLE_SELECT)
    .eq("id", id)
    .maybeSingle();

  if (error) {
    throw new Error(`Не удалось загрузить статью: ${error.message}`);
  }
  if (!data) {
    return null;
  }
  return toArticle(data as unknown as ArticleRow);
}
