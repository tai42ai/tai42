/** What an absent cell shows. */
export const EMPTY_PLACEHOLDER = '—';

/** An ISO instant in the reader's locale, or the value as given when it does not parse. */
export function formatInstant(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}
