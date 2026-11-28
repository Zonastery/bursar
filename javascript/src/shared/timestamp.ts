/** Normalize an RFC 3339 timestamp without discarding PostgreSQL microseconds. */
export function utcTimestamp(value: string): string {
  const match =
    /^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}(?::?\d{2})?)$/i.exec(
      value,
    );
  if (!match) throw new Error(`Invalid timestamp: ${value}`);
  const iso = value.replace(" ", "T");
  const normalized = /[+-]\d{2}$/.test(iso) ? `${iso}:00` : iso;
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) throw new Error(`Invalid timestamp: ${value}`);
  const fraction = match[2] ?? "";
  return `${parsed.toISOString().slice(0, 19)}.${fraction.padEnd(fraction.length > 3 ? 6 : 3, "0")}Z`;
}
