import { vi } from 'vitest';

import type { FormReactions } from '@/transcript-model';

// The reaction-door URL template the page door writes onto `#root` — the per-interaction
// react sibling of this plugin's own answer door, with the `{interaction_id}` placeholder.
const REACTION_ENDPOINT = '/api/channels/web/questions/{interaction_id}/react';

/** A mount carrying the reaction-endpoint template, so `reactToForm` finds its door. */
export function mountRoot(): void {
  const root = document.createElement('div');
  root.id = 'root';
  root.dataset.reactionEndpoint = REACTION_ENDPOINT;
  document.body.appendChild(root);
}

/** A reaction door 2xx carrying a form update under the platform `{data}` envelope. */
export function okUpdate(update: Record<string, unknown>): Response {
  return {
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify({ data: update })),
  } as Response;
}

/** A full reactions declaration from the parts a case cares about. */
export function reactions(overrides: Partial<FormReactions> = {}): FormReactions {
  return { fieldChanged: [], pageAdvanced: [], submitted: false, choices: [], ...overrides };
}

export function stubFetch(response: Response): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}
