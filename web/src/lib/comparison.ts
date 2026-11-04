import { resolveActiveEventId } from "./events";
import { supabase } from "./supabase";

export type ClaimBucket = "confirmed" | "unique" | "disputed";

export type ComparisonMember = {
  sourceName: string;
  sourceUrl: string;
  text: string;
  evidence: string;
};

export type ComparisonFact = {
  groupId: string;
  bucket: ClaimBucket;
  // Текст самого раннего claim в группе (по joined_at) — формулировка "как
  // факт впервые попал в группу", используется как подпись строки для
  // confirmed/unique; для disputed строка всё равно раскрывает все members
  // отдельно, представление не используется как единственная правда.
  representativeText: string;
  sourceNames: string[];
  members: ComparisonMember[];
};

export type EventComparison = {
  sources: string[];
  facts: ComparisonFact[];
};

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// PostgREST типизирует to-one embed как объект-или-массив — см. events.ts.
function one<T>(v: T | T[] | null | undefined): T | null {
  if (!v) return null;
  return Array.isArray(v) ? (v[0] ?? null) : v;
}

type SourceRow = { name: string };
type ArticleRow = { canonical_url: string; sources: SourceRow | SourceRow[] | null };
type ClaimRow = { text: string; evidence: string; articles: ArticleRow | ArticleRow[] | null };
type GroupMemberRow = { relation: string; joined_at: string; claims: ClaimRow | ClaimRow[] | null };
type ClaimGroupRow = {
  id: string;
  bucket: ClaimBucket;
  created_at: string;
  claim_group_members: GroupMemberRow[] | null;
};

function toFact(group: ClaimGroupRow): ComparisonFact | null {
  const members: ComparisonMember[] = [];
  let earliest: { joinedAt: string; text: string } | null = null;

  for (const m of group.claim_group_members ?? []) {
    const claim = one(m.claims);
    const article = claim ? one(claim.articles) : null;
    const source = article ? one(article.sources) : null;
    if (!claim || !article || !source) continue;

    members.push({
      sourceName: source.name,
      sourceUrl: article.canonical_url,
      text: claim.text,
      evidence: claim.evidence,
    });

    if (!earliest || m.joined_at < earliest.joinedAt) {
      earliest = { joinedAt: m.joined_at, text: claim.text };
    }
  }

  if (members.length === 0 || !earliest) return null;

  return {
    groupId: group.id,
    bucket: group.bucket,
    representativeText: earliest.text,
    sourceNames: [...new Set(members.map((m) => m.sourceName))],
    members,
  };
}

export async function getEventComparison(id: string): Promise<EventComparison | null> {
  if (!UUID_RE.test(id)) {
    return null;
  }

  const activeId = await resolveActiveEventId(id);
  if (!activeId) {
    return null;
  }

  const { data, error } = await supabase
    .from("claim_groups")
    .select(
      "id, bucket, created_at, claim_group_members(relation, joined_at," +
        " claims(text, evidence, articles(canonical_url, sources(name))))"
    )
    .eq("event_id", activeId)
    .order("created_at", { ascending: true });

  if (error) {
    throw new Error(`Не удалось загрузить сравнение подач: ${error.message}`);
  }

  const facts = ((data ?? []) as unknown as ClaimGroupRow[])
    .map(toFact)
    .filter((f): f is ComparisonFact => f !== null);

  const sources = [...new Set(facts.flatMap((f) => f.sourceNames))];

  // Меньше 2 источников — сравнивать нечего (один источник сам с собой не
  // расходится), показываем "недостаточно данных" вместо вырожденной таблицы.
  if (facts.length === 0 || sources.length < 2) {
    return null;
  }

  return { sources, facts };
}
