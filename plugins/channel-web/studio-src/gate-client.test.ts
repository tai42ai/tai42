/**
 * The four gate calls over a fake host request: each builds its path on the given mount
 * base (identity and code id encoded once), sends its method and body, and hands the
 * request the door's own schema.
 */
import { describe, expect, it, vi } from 'vitest';

import { type ApiRequest, getGate, mintCode, revokeCode, setGate } from './gate-client';
import {
  webEntryCodeMinted,
  webEntryCodeRevoked,
  webEntryGate,
  webEntryGateState,
} from './gate-schemas';

function fakeRequest(answer: unknown) {
  const request = vi.fn().mockResolvedValue(answer);
  return { request: request as unknown as ApiRequest, spy: request };
}

describe('gate client', () => {
  it('reads the gate under the base with the gate schema', async () => {
    const { request, spy } = fakeRequest({ enabled: true, codes: [] });
    await getGate(request, '/api/chat-alt', 'a/b c');
    expect(spy).toHaveBeenCalledWith('/api/chat-alt/gates/a%2Fb%20c', webEntryGate, {
      signal: undefined,
    });
  });

  it('flips the flag with a PUT', async () => {
    const { request, spy } = fakeRequest({ enabled: false });
    await setGate(request, '/api/channels/web', 'id', false);
    expect(spy).toHaveBeenCalledWith('/api/channels/web/gates/id', webEntryGateState, {
      method: 'PUT',
      body: { enabled: false },
    });
  });

  it('mints with both body fields sent explicitly', async () => {
    const { request, spy } = fakeRequest({ code: 'x', code_id: 'c', expires_at: null });
    await mintCode(request, '/api/channels/web', 'id', { label: null, expires_at: null });
    expect(spy).toHaveBeenCalledWith('/api/channels/web/gates/id/codes', webEntryCodeMinted, {
      method: 'POST',
      body: { label: null, expires_at: null },
    });
  });

  it('revokes a code by its encoded id with a DELETE', async () => {
    const { request, spy } = fakeRequest({ status: 'revoked' });
    await revokeCode(request, '/api/channels/web', 'id', 'c/1');
    expect(spy).toHaveBeenCalledWith(
      '/api/channels/web/gates/id/codes/c%2F1',
      webEntryCodeRevoked,
      {
        method: 'DELETE',
      },
    );
  });
});
