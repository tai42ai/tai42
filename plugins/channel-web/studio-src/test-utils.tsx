/**
 * Test harness: render the entry-gate panel in the provider stack the Studio supplies —
 * the SDK's `ApiProvider` fed a stub client and the theme. The stub's authed `request`
 * routes each gate door (by path and method under the mount base) to a per-door spy with
 * the door's own arguments, and parses the spy's answer with the caller's schema, as the
 * host does.
 */
import {
  type ApiClient,
  ApiProvider,
  type ChannelConversationRoute,
  ThemeProvider,
} from '@tai42/studio-sdk';
import { render, type RenderResult } from '@testing-library/react';
import type { ReactElement } from 'react';
import { type Mock, vi } from 'vitest';
import type { z } from 'zod';

import { EntryGatePanel } from './EntryGatePanel';
import { setGateBase } from './gate-base';

export const MOUNT_BASE = '/api/channels/web';

/** The four gate doors as spies with the door's own arguments. */
export interface GateDoors {
  readonly getGate?: Mock;
  readonly setGate?: Mock;
  readonly mintCode?: Mock;
  readonly revokeCode?: Mock;
}

/** A stub authed `request` dispatching to the door spies under `base`. */
export function gateRequest(doors: GateDoors, base = MOUNT_BASE) {
  return vi.fn(
    async (
      path: string,
      schema: z.ZodType,
      options: { method?: string; body?: unknown; signal?: AbortSignal } = {},
    ) => {
      if (!path.startsWith(`${base}/gates/`)) throw new Error(`unexpected path ${path}`);
      const [identity, codes, codeId] = path
        .slice(`${base}/gates/`.length)
        .split('/')
        .map(decodeURIComponent);
      const method = options.method ?? 'GET';
      let answer: unknown;
      if (codes === undefined && method === 'GET') {
        answer = await doors.getGate?.(identity, options.signal);
      } else if (codes === undefined && method === 'PUT') {
        answer = await doors.setGate?.(identity, (options.body as { enabled: boolean }).enabled);
      } else if (codeId === undefined && method === 'POST') {
        answer = await doors.mintCode?.(identity, options.body);
      } else if (method === 'DELETE') {
        answer = await doors.revokeCode?.(identity, codeId);
      } else {
        throw new Error(`unexpected ${method} ${path}`);
      }
      return schema.parse(answer);
    },
  );
}

export function webRoute(
  overrides: Partial<ChannelConversationRoute> = {},
): ChannelConversationRoute {
  return {
    route_name: 'web-chat',
    door: 'channel',
    channel: 'web',
    our_identity: 'web-ident',
    target_kind: 'agent',
    target_name: 'assistant',
    start_expr: null,
    reply_expr: null,
    cancel_expr: null,
    resume_expr: null,
    extras_expr: null,
    initial_mode: 'agent',
    execution_key: 'svc-chat',
    callback_url: null,
    turns_per_hour_override: null,
    error_reply_text: null,
    execution_key_fingerprint: 'fp-1',
    locale: null,
    overlap: { running: 'continue', deliver: 'one', settle_seconds: 0 },
    ...overrides,
  } as ChannelConversationRoute;
}

export function makeCode(overrides: Record<string, unknown> = {}) {
  return {
    code_id: 'c1',
    label: 'newsletter',
    created_at: '2026-08-01T09:00:00Z',
    expires_at: null as string | null,
    ...overrides,
  };
}

/** A stub client carrying the authed request (and the origin the chat link composes on). */
export function stubClient(request: ReturnType<typeof gateRequest>, baseUrl = ''): ApiClient {
  return { baseUrl, request } as unknown as ApiClient;
}

export function renderWithClient(ui: ReactElement, client: ApiClient): RenderResult {
  return render(
    <ApiProvider value={client}>
      <ThemeProvider>{ui}</ThemeProvider>
    </ApiProvider>,
  );
}

/** Render the panel for the default web route under a captured mount base. */
export function renderPanel(
  doors: GateDoors,
  { base = MOUNT_BASE, baseUrl = '' }: { base?: string | null; baseUrl?: string } = {},
) {
  setGateBase(base);
  const request = gateRequest(doors, base ?? MOUNT_BASE);
  const result = renderWithClient(
    <EntryGatePanel pluginId="tai42_channel_web" route={webRoute()} />,
    stubClient(request, baseUrl),
  );
  return { ...result, request };
}
