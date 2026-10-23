/**
 * The three words calls, over the host's authed request (`useApi().request`) and the
 * channel item's mount base captured at register. Plain functions taking the request
 * function first, so they are callable outside a component; the hook that owns them reads
 * `useApi()` and passes its request in.
 */
import type { ApiRequest } from './gate-client';
import { type WebWords, webWords, webWordsCleared } from './words-schemas';

function wordsPath(base: string, identity: string): string {
  return `${base}/words/${encodeURIComponent(identity)}`;
}

/** Whether `error` is the host's api error for a 404 (recognised by name, as the SDK
 *  recognises the api-client's errors across the bundle boundary). */
function isNotFound(error: unknown): boolean {
  return (
    error instanceof Error &&
    error.name === 'ApiError' &&
    (error as Error & { status?: unknown }).status === 404
  );
}

/** Read the route's words; `null` when the route set none (the door answers 404). Every
 *  other failure propagates. */
export async function getWords(
  request: ApiRequest,
  base: string,
  identity: string,
  signal?: AbortSignal,
): Promise<WebWords | null> {
  try {
    return await request(wordsPath(base, identity), webWords, { signal });
  } catch (error) {
    if (isNotFound(error)) return null;
    throw error;
  }
}

/** Replace the route's whole words object; the answer is what is now stored. */
export function replaceWords(request: ApiRequest, base: string, identity: string, words: WebWords) {
  return request(wordsPath(base, identity), webWords, { method: 'PUT', body: words });
}

/** Clear the route's words, so every page and sentence draws the built-in English. */
export function clearWords(request: ApiRequest, base: string, identity: string) {
  return request(wordsPath(base, identity), webWordsCleared, { method: 'DELETE' });
}
