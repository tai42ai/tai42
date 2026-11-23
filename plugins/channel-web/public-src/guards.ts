/** The type guards the wire parsers and the form widget share. */

/** A plain JS object (not an array, not `null`), or not. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
