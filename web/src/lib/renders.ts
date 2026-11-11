import { resolveActiveEventId } from "./events";
import { clampQuote } from "./quote";
import { supabase } from "./supabase";

export type ClaimBucket = "confirmed" | "unique" | "disputed";

export type CitedNumber = {
  value: number;
  unit: string | null;
  label: string | null;
};

export type CitedClaim = {
  claimId: string;
  evidence: string;
  sourceName: string;
  sourceUrl: string;
  // null = claim ещё не прошёл group_claims (не покрыт группировкой), а не
  // "подтверждён одним источником" — поэтому бейдж в этом случае не рисуется,
  // см. events/[id]/page.tsx. Дефолтить в 'unique' здесь нельзя: при ~15%
  // покрытия это было бы ложным утверждением для большинства claims.
  bucket: ClaimBucket | null;
  sourceCount: number | null;
  numbers: CitedNumber[];
};

export type RenderSentence = {
  text: string;
  claims: CitedClaim[];
};

export type EventRender = {
  id: string;
  updatedAt: string;
  sentences: RenderSentence[];
};

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// PostgREST типизирует to-one embed как объект-или-массив — см. events.ts.
function one<T>(v: T | T[] | null | undefined): T | null {
  if (!v) return null;
  return Array.isArray(v) ? (v[0] ?? null) : v;
}

type SourceRow = { name: string };
type ArticleRow = { canonical_url: string; sources: SourceRow | SourceRow[] | null };
type ClaimGroupRow = { bucket: ClaimBucket; source_count: number };
type ClaimGroupMemberRow = { claim_groups: ClaimGroupRow | ClaimGroupRow[] | null };
type ClaimNumberRow = { value: number; unit: string | null; label: string | null };

type ClaimRow = {
  id: string;
  evidence: string;
  articles: ArticleRow | ArticleRow[] | null;
  claim_group_members: ClaimGroupMemberRow | ClaimGroupMemberRow[] | null;
  claim_numbers: ClaimNumberRow[] | null;
};

type RenderClaimRow = {
  sentence_index: number;
  claims: ClaimRow | ClaimRow[] | null;
};

function toCitedClaim(claim: ClaimRow): CitedClaim | null {
  const article = one(claim.articles);
  const source = article ? one(article.sources) : null;
  if (!article || !source) return null;

  const groupMember = one(claim.claim_group_members);
  const group = groupMember ? one(groupMember.claim_groups) : null;

  return {
    claimId: claim.id,
    evidence: clampQuote(claim.evidence),
    sourceName: source.name,
    sourceUrl: article.canonical_url,
    bucket: group?.bucket ?? null,
    sourceCount: group?.source_count ?? null,
    numbers: (claim.claim_numbers ?? []).map((n) => ({
      value: n.value,
      unit: n.unit,
      label: n.label,
    })),
  };
}

function assembleRender(
  render: { id: string; body: string; updated_at: string },
  rows: RenderClaimRow[]
): EventRender {
  const claimsBySentence = new Map<number, CitedClaim[]>();
  for (const row of rows) {
    const claim = one(row.claims);
    if (!claim) continue;
    const cited = toCitedClaim(claim);
    if (!cited) continue;
    const list = claimsBySentence.get(row.sentence_index) ?? [];
    list.push(cited);
    claimsBySentence.set(row.sentence_index, list);
  }

  const lines = render.body.trim().length > 0 ? render.body.split("\n") : [];
  return {
    id: render.id,
    updatedAt: render.updated_at,
    sentences: lines.map((text, i) => ({
      text,
      claims: claimsBySentence.get(i) ?? [],
    })),
  };
}

export async function getEventRender(
  id: string,
  format: "summary_30s" | "full_breakdown"
): Promise<EventRender | null> {
  if (!UUID_RE.test(id)) {
    return null;
  }

  const activeId = await resolveActiveEventId(id);
  if (!activeId) {
    return null;
  }

  const { data: render, error: renderError } = await supabase
    .from("renders")
    .select("id, body, updated_at")
    .eq("event_id", activeId)
    .eq("format", format)
    .eq("lang", "ru")
    .maybeSingle();

  if (renderError) {
    throw new Error(`Не удалось загрузить рендер события: ${renderError.message}`);
  }
  // Нет рендера — нормальный случай при неполном покрытии синтеза, а не ошибка.
  if (!render) {
    return null;
  }

  const { data: claimRows, error: claimsError } = await supabase
    .from("render_claims")
    .select(
      "sentence_index, claims(id, evidence, articles(canonical_url, sources(name))," +
        " claim_group_members(claim_groups(bucket, source_count)), claim_numbers(value, unit, label))"
    )
    .eq("render_id", render.id)
    .order("sentence_index", { ascending: true });

  if (claimsError) {
    throw new Error(`Не удалось загрузить источники рендера: ${claimsError.message}`);
  }

  return assembleRender(render, (claimRows ?? []) as unknown as RenderClaimRow[]);
}
