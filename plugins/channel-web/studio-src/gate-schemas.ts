/**
 * The web channel's entry-gate answers. A web route can be gated so its chat page is
 * served only to a navigation carrying a live entry code; the four gate doors are keyed by
 * the route's `our_identity`. A raw code exists only in flight (the mint answer); a listed
 * code carries its hash id, never a raw value (none is stored).
 */
import { z } from 'zod';

/** One minted entry code as the gate read lists it — its hash id, its optional label, and
 *  when it was created / dies. Never a raw code. */
export const webEntryCode = z.object({
  code_id: z.string(),
  label: z.string().nullable(),
  created_at: z.string(),
  expires_at: z.string().nullable(),
});
export type WebEntryCode = z.infer<typeof webEntryCode>;

/** The gate read — the gate flag and its live codes. */
export const webEntryGate = z.object({
  enabled: z.boolean(),
  codes: z.array(webEntryCode),
});
export type WebEntryGate = z.infer<typeof webEntryGate>;

/** The gate flag after a write. */
export const webEntryGateState = z.object({ enabled: z.boolean() });

/**
 * A freshly minted code. The raw `code` rides this answer ONCE (nothing else stores or
 * lists it); `code_id` is its hash id and `expires_at` an ISO-8601 instant, or `null` for a
 * code that never expires.
 */
export const webEntryCodeMinted = z.object({
  code: z.string(),
  code_id: z.string(),
  expires_at: z.string().nullable(),
});
export type WebEntryCodeMinted = z.infer<typeof webEntryCodeMinted>;

/** The revoked code. */
export const webEntryCodeRevoked = z.object({ status: z.literal('revoked') });
