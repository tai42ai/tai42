/**
 * The three words calls over a fake host request: each builds its path on the given mount
 * base (the identity encoded once), sends its method and body, and hands the request the
 * door's own schema. The read answers `null` for the door's 404 (no words set) and lets
 * every other failure through.
 */
import { describe, expect, it, vi } from 'vitest';

import type { ApiRequest } from './gate-client';
import { clearWords, getWords, replaceWords } from './words-client';
import { webWords, webWordsCleared } from './words-schemas';

function fakeRequest(answer: unknown) {
  const request = vi.fn().mockResolvedValue(answer);
  return { request: request as unknown as ApiRequest, spy: request };
}

function failingRequest(error: Error) {
  return vi.fn().mockRejectedValue(error) as unknown as ApiRequest;
}

function apiError(message: string, status: number): Error {
  return Object.assign(new Error(message), { name: 'ApiError', status });
}

describe('words client', () => {
  it('reads the words under the base with the words schema', async () => {
    const { request, spy } = fakeRequest({ transport: { busy: 'Wait.' } });
    expect(await getWords(request, '/api/chat-alt', 'a/b c')).toEqual({
      transport: { busy: 'Wait.' },
    });
    expect(spy).toHaveBeenCalledWith('/api/chat-alt/words/a%2Fb%20c', webWords, {
      signal: undefined,
    });
  });

  it('reads no words as null when the door answers 404', async () => {
    const request = failingRequest(apiError('no words are set for this web route', 404));
    expect(await getWords(request, '/api/channels/web', 'id')).toBeNull();
  });

  it.each([
    ['another status', apiError('boom', 500)],
    ['a non-api error', new Error('network down')],
  ])('lets a read failure through (%s)', async (_label, error) => {
    await expect(getWords(failingRequest(error), '/api/channels/web', 'id')).rejects.toBe(error);
  });

  it('replaces the whole words object with a PUT', async () => {
    const words = { page: { entry_refused: { heading: 'Private' } } };
    const { request, spy } = fakeRequest(words);
    await replaceWords(request, '/api/channels/web', 'id', words);
    expect(spy).toHaveBeenCalledWith('/api/channels/web/words/id', webWords, {
      method: 'PUT',
      body: words,
    });
  });

  it('clears the words with a DELETE', async () => {
    const { request, spy } = fakeRequest({ status: 'cleared' });
    await clearWords(request, '/api/channels/web', 'id');
    expect(spy).toHaveBeenCalledWith('/api/channels/web/words/id', webWordsCleared, {
      method: 'DELETE',
    });
  });
});
