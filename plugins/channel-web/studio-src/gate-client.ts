/**
 * The four entry-gate calls, over the host's authed request (`useApi().request`) and the
 * channel item's mount base captured at register. Plain functions taking the request
 * function first, so they are callable outside a component; the hook that owns them reads
 * `useApi()` and passes its request in.
 */
import type { ApiClient } from '@tai42/studio-sdk';

import {
  webEntryCodeMinted,
  webEntryCodeRevoked,
  webEntryGate,
  webEntryGateState,
} from './gate-schemas';

export type ApiRequest = ApiClient['request'];

/** The mint body. Both fields are sent EXPLICITLY: `label` a human tag (`null` for none),
 *  `expires_at` an ISO-8601 instant the code dies at (`null` for never). */
export interface MintBody {
  readonly label: string | null;
  readonly expires_at: string | null;
}

function gatePath(base: string, identity: string): string {
  return `${base}/gates/${encodeURIComponent(identity)}`;
}

/** Read the gate flag and its live codes. */
export function getGate(request: ApiRequest, base: string, identity: string, signal?: AbortSignal) {
  return request(gatePath(base, identity), webEntryGate, { signal });
}

/** Flip the gate flag. */
export function setGate(request: ApiRequest, base: string, identity: string, enabled: boolean) {
  return request(gatePath(base, identity), webEntryGateState, { method: 'PUT', body: { enabled } });
}

/** Mint a code; the raw code rides the answer once. */
export function mintCode(request: ApiRequest, base: string, identity: string, body: MintBody) {
  return request(`${gatePath(base, identity)}/codes`, webEntryCodeMinted, {
    method: 'POST',
    body: { label: body.label, expires_at: body.expires_at },
  });
}

/** Revoke a code by its hash id (immediate and durable). */
export function revokeCode(request: ApiRequest, base: string, identity: string, codeId: string) {
  return request(
    `${gatePath(base, identity)}/codes/${encodeURIComponent(codeId)}`,
    webEntryCodeRevoked,
    { method: 'DELETE' },
  );
}
