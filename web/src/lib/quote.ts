// Раздел 3.1 CLAUDE.md: пользователю показываются цитаты не длиннее одного
// предложения. Воркер отбрасывает длинные evidence при извлечении
// (claim_extraction.MAX_EVIDENCE_CHARS), но в БД остались более ранние записи —
// поэтому показ ограничен и здесь, на сервере, до отправки в браузер.
export const MAX_QUOTE_CHARS = 300;

export function clampQuote(text: string, max: number = MAX_QUOTE_CHARS): string {
  const normalized = text.replace(/\s+/g, " ").trim();
  if (normalized.length <= max) return normalized;

  const cut = normalized.slice(0, max);
  const lastSpace = cut.lastIndexOf(" ");
  // Режем по границе слова, если она не слишком далеко от лимита.
  const body = lastSpace > max * 0.6 ? cut.slice(0, lastSpace) : cut;
  return `${body.replace(/[\s,;:—–-]+$/, "")}…`;
}
