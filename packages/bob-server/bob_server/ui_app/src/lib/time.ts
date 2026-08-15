/**
 * Parse a timestamp from the calls API into a Date, robust to the formats in
 * the DB: SQLite `YYYY-MM-DD HH:MM:SS` (UTC, no zone), ISO with `+00:00`,
 * and ISO with `Z`. Returns Invalid Date only for null/empty input.
 */
export function parseTs(ts: string | null | undefined): Date {
  if (!ts) return new Date(NaN);
  if (ts.endsWith("Z") || ts.includes("+")) return new Date(ts);
  // SQLite format: space separator, UTC assumed — make it ISO and tag UTC.
  return new Date(ts.replace(" ", "T") + "Z");
}
