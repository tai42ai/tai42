/**
 * The entry-gate panel's query cache. The host's cache is not shared across the plugin
 * boundary, so the panel builds its own with the host's two rules: nothing retries (a
 * failure shows at once), and a 401 from any gate read or change routes to sign-in through
 * the SDK's `useOnUnauthorized()` handler. The api-client's `ApiUnauthorizedError` is
 * recognised by name, as the SDK recognises it across the bundle boundary.
 */
import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query';

function isUnauthorized(error: unknown): boolean {
  return error instanceof Error && error.name === 'ApiUnauthorizedError';
}

export function gateQueryClient(onUnauthorized: () => void): QueryClient {
  const onError = (error: unknown): void => {
    if (isUnauthorized(error)) onUnauthorized();
  };
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    queryCache: new QueryCache({ onError }),
    mutationCache: new MutationCache({ onError }),
  });
}
