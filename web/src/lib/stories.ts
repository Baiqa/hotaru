import { supabase } from "./supabase";

export type StoryEventItem = {
  eventId: string;
  title: string;
  startedAt: string;
  position: number;
};

export type Story = {
  id: string;
  title: string;
  events: StoryEventItem[];
};

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// PostgREST типизирует to-one embed как объект-или-массив — см. events.ts.
function one<T>(v: T | T[] | null | undefined): T | null {
  if (!v) return null;
  return Array.isArray(v) ? (v[0] ?? null) : v;
}

type EventRow = { id: string; title: string; started_at: string };
type StoryEventRow = { position: number; events: EventRow | EventRow[] | null };
type StoryRow = { id: string; title: string; story_events: StoryEventRow[] | null };

export async function getStoryById(id: string): Promise<Story | null> {
  if (!UUID_RE.test(id)) {
    return null;
  }

  const { data, error } = await supabase
    .from("stories")
    .select("id, title, story_events(position, events(id, title, started_at))")
    .eq("id", id)
    .eq("status", "active")
    .order("position", { referencedTable: "story_events", ascending: true })
    .maybeSingle();

  if (error) {
    throw new Error(`Не удалось загрузить сюжет: ${error.message}`);
  }
  if (!data) {
    return null;
  }

  const row = data as unknown as StoryRow;
  const events = (row.story_events ?? [])
    .map((se) => {
      const ev = one(se.events);
      if (!ev) return null;
      return { eventId: ev.id, title: ev.title, startedAt: ev.started_at, position: se.position };
    })
    .filter((e): e is StoryEventItem => e !== null);

  return { id: row.id, title: row.title, events };
}

// Этап 4: ссылка "Сюжет: ... ->" на странице события — только если событие уже
// дошло до hotaru-assign-stories. В отличие от рендера/сравнения, здесь нет
// смысла в "недостаточно данных": ссылка либо есть, либо её нет вовсе.
export async function getStoryForEvent(eventId: string): Promise<{ id: string; title: string } | null> {
  if (!UUID_RE.test(eventId)) {
    return null;
  }

  const { data, error } = await supabase
    .from("story_events")
    .select("stories(id, title)")
    .eq("event_id", eventId)
    .maybeSingle();

  if (error) {
    throw new Error(`Не удалось найти сюжет события: ${error.message}`);
  }
  if (!data) {
    return null;
  }

  type Row = { stories: { id: string; title: string } | { id: string; title: string }[] | null };
  return one((data as unknown as Row).stories);
}
