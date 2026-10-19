import { supabase } from "./supabase";

export type EventSummary = {
  id: string;
  title: string;
  startedAt: string;
  updatedAt: string;
  sourceNames: string[];
  articleCount: number;
};

export type EventMember = {
  articleId: string;
  title: string;
  lead: string | null;
  sourceName: string;
  publishedAt: string | null;
};

export type EventDetail = {
  id: string;
  title: string;
  startedAt: string;
  updatedAt: string;
  members: EventMember[];
};

type SourceNameRow = { name: string };

type ArticleWithSourceRow = {
  title?: string;
  lead?: string | null;
  published_at?: string | null;
  sources: SourceNameRow | SourceNameRow[] | null;
};

type EventArticleRow = {
  article_id: string;
  articles: ArticleWithSourceRow | ArticleWithSourceRow[] | null;
};

type EventListRow = {
  id: string;
  title: string;
  started_at: string;
  updated_at: string;
  event_articles: EventArticleRow[] | null;
};

const EVENT_LIST_LIMIT = 30;
const MAX_CANONICAL_HOPS = 5;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// PostgREST типизирует to-one embed как объект-или-массив в зависимости от того,
// может ли он однозначно определить связь — на практике тут всегда один элемент.
function one<T>(v: T | T[] | null | undefined): T | null {
  if (!v) return null;
  return Array.isArray(v) ? v[0] ?? null : v;
}

function sourceNamesOf(row: EventListRow): string[] {
  const names = new Set<string>();
  for (const ea of row.event_articles ?? []) {
    const article = one(ea.articles);
    const source = article ? one(article.sources) : null;
    if (source) names.add(source.name);
  }
  return Array.from(names);
}

function toEventSummary(row: EventListRow): EventSummary {
  return {
    id: row.id,
    title: row.title,
    startedAt: row.started_at,
    updatedAt: row.updated_at,
    sourceNames: sourceNamesOf(row),
    articleCount: (row.event_articles ?? []).length,
  };
}

export async function listEvents(): Promise<EventSummary[]> {
  const { data, error } = await supabase
    .from("events")
    .select("id, title, started_at, updated_at, event_articles(articles(sources(name)))")
    .eq("status", "active")
    .order("updated_at", { ascending: false })
    .limit(EVENT_LIST_LIMIT);

  if (error) {
    throw new Error(`Не удалось загрузить список событий: ${error.message}`);
  }
  return (data as unknown as EventListRow[]).map(toEventSummary);
}

function toEventDetail(row: EventListRow): EventDetail {
  const members = (row.event_articles ?? [])
    .map((ea) => {
      const article = one(ea.articles);
      if (!article) return null;
      const source = one(article.sources);
      return {
        articleId: ea.article_id,
        title: article.title ?? "",
        lead: article.lead ?? null,
        sourceName: source?.name ?? "Неизвестный источник",
        publishedAt: article.published_at ?? null,
      };
    })
    .filter((m): m is EventMember => m !== null)
    .sort((a, b) => {
      if (!a.publishedAt && !b.publishedAt) return 0;
      if (!a.publishedAt) return 1;
      if (!b.publishedAt) return -1;
      return b.publishedAt.localeCompare(a.publishedAt);
    });

  return {
    id: row.id,
    title: row.title,
    startedAt: row.started_at,
    updatedAt: row.updated_at,
    members,
  };
}

// Раздел 3.3 CLAUDE.md: у события есть canonical_id, "старые ссылки не должны
// умирать". Поглощённое при merge событие остаётся в таблице со status='merged'
// и пустым event_articles (их перенесли на канонического победителя) — поэтому
// сюда нельзя просто сходить по id, нужно сначала дойти до живого события.
async function resolveActiveEventId(id: string): Promise<string | null> {
  let currentId = id;
  for (let hop = 0; hop < MAX_CANONICAL_HOPS; hop++) {
    const { data, error } = await supabase
      .from("events")
      .select("id, status, canonical_id")
      .eq("id", currentId)
      .maybeSingle();

    if (error) {
      throw new Error(`Не удалось проверить событие: ${error.message}`);
    }
    if (!data) {
      return null;
    }
    if (data.status === "active") {
      return data.id;
    }
    if (!data.canonical_id) {
      return null;
    }
    currentId = data.canonical_id;
  }
  return null;
}

export async function getEventById(id: string): Promise<EventDetail | null> {
  if (!UUID_RE.test(id)) {
    return null;
  }

  const activeId = await resolveActiveEventId(id);
  if (!activeId) {
    return null;
  }

  const { data, error } = await supabase
    .from("events")
    .select(
      "id, title, started_at, updated_at, event_articles(article_id, articles(title, lead, published_at, sources(name)))"
    )
    .eq("id", activeId)
    .maybeSingle();

  if (error) {
    throw new Error(`Не удалось загрузить событие: ${error.message}`);
  }
  if (!data) {
    return null;
  }
  return toEventDetail(data as unknown as EventListRow);
}

export function sourceWord(count: number): string {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return "источник";
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "источника";
  return "источников";
}

export async function getEventIdForArticle(articleId: string): Promise<string | null> {
  const { data, error } = await supabase
    .from("event_articles")
    .select("event_id")
    .eq("article_id", articleId)
    .maybeSingle();

  if (error) {
    throw new Error(`Не удалось найти событие статьи: ${error.message}`);
  }
  return data?.event_id ?? null;
}
