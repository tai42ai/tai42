/**
 * Test harness: render the web channel's route panels in the provider stack the Studio
 * supplies — the SDK's `ApiProvider` fed a stub client and the theme, plus (for the
 * composite web route panel) a signed-in `AuthProvider` and the `CapabilityProvider` over
 * the stub's projection. The stub's authed
 * `request` routes each gate and words door (by path and method under the mount base) to
 * a per-door spy with the door's own arguments, and parses the spy's answer with the
 * caller's schema, as the host does.
 */
import {
  type ApiClient,
  ApiProvider,
  AuthProvider,
  CapabilityProvider,
  type ChannelConversationRoute,
  ThemeProvider,
  UnauthorizedProvider,
  useCapabilities,
} from '@tai42/studio-sdk';
import { render, type RenderResult } from '@testing-library/react';
import type { ReactElement, ReactNode } from 'react';
import { type Mock, vi } from 'vitest';
import type { z } from 'zod';

import { EntryGatePanel } from './EntryGatePanel';
import { setMountBase } from './mount-base';
import { WordsPanel } from './WordsPanel';

export const MOUNT_BASE = '/api/channels/web';

/** The four gate doors as spies with the door's own arguments. */
export interface GateDoors {
  readonly getGate?: Mock;
  readonly setGate?: Mock;
  readonly mintCode?: Mock;
  readonly revokeCode?: Mock;
}

/** The three words doors as spies with the door's own arguments. */
export interface WordsDoors {
  readonly getWords?: Mock;
  readonly replaceWords?: Mock;
  readonly clearWords?: Mock;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
}

async function wordsAnswer(
  doors: WordsDoors,
  rest: string,
  options: RequestOptions,
): Promise<unknown> {
  const parts = rest.split('/');
  if (parts.length !== 1) throw new Error(`unexpected words path ${rest}`);
  const identity = decodeURIComponent(parts[0] ?? '');
  const method = options.method ?? 'GET';
  if (method === 'GET') return doors.getWords?.(identity, options.signal);
  if (method === 'PUT') return doors.replaceWords?.(identity, options.body);
  if (method === 'DELETE') return doors.clearWords?.(identity);
  throw new Error(`unexpected ${method} words/${rest}`);
}

async function gateAnswer(
  doors: GateDoors,
  base: string,
  path: string,
  options: RequestOptions,
): Promise<unknown> {
  if (!path.startsWith(`${base}/gates/`)) throw new Error(`unexpected path ${path}`);
  const [identity, codes, codeId] = path
    .slice(`${base}/gates/`.length)
    .split('/')
    .map(decodeURIComponent);
  const method = options.method ?? 'GET';
  if (codes === undefined && method === 'GET') return doors.getGate?.(identity, options.signal);
  if (codes === undefined && method === 'PUT') {
    return doors.setGate?.(identity, (options.body as { enabled: boolean }).enabled);
  }
  if (codeId === undefined && method === 'POST') return doors.mintCode?.(identity, options.body);
  if (method === 'DELETE') return doors.revokeCode?.(identity, codeId);
  throw new Error(`unexpected ${method} ${path}`);
}

/** A stub authed `request` dispatching to the gate and words door spies under `base`. */
export function doorRequest(doors: GateDoors & WordsDoors, base = MOUNT_BASE) {
  return vi.fn(async (path: string, schema: z.ZodType, options: RequestOptions = {}) => {
    if (path.startsWith(`${base}/words/`)) {
      return schema.parse(await wordsAnswer(doors, path.slice(`${base}/words/`.length), options));
    }
    return schema.parse(await gateAnswer(doors, base, path, options));
  });
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
    reply_texts: null,
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

/** The caller's capability projection, as `GET /api/auth/me` answers it. */
export type Projection = Awaited<ReturnType<ApiClient['getMe']>>;

/** A projection reaching exactly the given route paths (a scoped session). */
export function scopedProjection(paths: readonly string[]): Projection {
  return {
    admin: false,
    routes: paths.map((path) => ({ path, methods: ['GET', 'PUT', 'POST', 'DELETE'] })),
    route_patterns: [],
  } as unknown as Projection;
}

/** The admin session's total projection. */
export const FULL_PROJECTION: Projection = {
  admin: true,
  routes: [],
  route_patterns: [],
} as unknown as Projection;

/** A stub client carrying the authed request, the origin the chat link composes on, and
 *  the caller's projection. */
export function stubClient(
  request: ReturnType<typeof doorRequest>,
  baseUrl = '',
  projection: Projection = FULL_PROJECTION,
): ApiClient {
  return { baseUrl, request, getMe: vi.fn().mockResolvedValue(projection) } as unknown as ApiClient;
}

/** Renders its children once the projection is ready, as the host's route-panel slot does. */
function OnceProjected({ children }: { readonly children: ReactNode }): ReactNode {
  const { state } = useCapabilities();
  return state.status === 'ready' ? children : null;
}

/**
 * Render `ui` under the Studio's providers. With `projected`, the stack also carries a
 * signed-in `AuthProvider` and the `CapabilityProvider` over the stub client's projection,
 * and `ui` renders once the projection is ready, as the host's route-panel slot does.
 */
export function renderWithClient(
  ui: ReactElement,
  client: ApiClient,
  onUnauthorized: () => void = vi.fn(),
  { projected = false }: { projected?: boolean } = {},
): RenderResult {
  const themed = (
    <ThemeProvider>{projected ? <OnceProjected>{ui}</OnceProjected> : ui}</ThemeProvider>
  );
  if (!projected) {
    return render(
      <UnauthorizedProvider value={onUnauthorized}>
        <ApiProvider value={client}>{themed}</ApiProvider>
      </UnauthorizedProvider>,
    );
  }
  // A signed-in session: the AuthProvider seeds its key from sessionStorage.
  globalThis.sessionStorage.setItem('tai-studio.apiKey', 'test-key');
  return render(
    <UnauthorizedProvider value={onUnauthorized}>
      <AuthProvider>
        <ApiProvider value={client}>
          <CapabilityProvider>{themed}</CapabilityProvider>
        </ApiProvider>
      </AuthProvider>
    </UnauthorizedProvider>,
  );
}

/** Render the panel for the default web route under a captured mount base. */
export function renderPanel(
  doors: GateDoors,
  {
    base = MOUNT_BASE,
    baseUrl = '',
    onUnauthorized = vi.fn(),
  }: { base?: string | null; baseUrl?: string; onUnauthorized?: Mock } = {},
) {
  setMountBase(base);
  const request = doorRequest(doors, base ?? MOUNT_BASE);
  const result = renderWithClient(
    <EntryGatePanel pluginId="tai42_channel_web" route={webRoute()} />,
    stubClient(request, baseUrl),
    onUnauthorized,
  );
  return { ...result, request, onUnauthorized };
}

/** Render the words panel for the default web route under a captured mount base. */
export function renderWordsPanel(
  doors: WordsDoors,
  {
    base = MOUNT_BASE,
    onUnauthorized = vi.fn(),
  }: { base?: string | null; onUnauthorized?: Mock } = {},
) {
  setMountBase(base);
  const request = doorRequest(doors, base ?? MOUNT_BASE);
  const result = renderWithClient(
    <WordsPanel pluginId="tai42_channel_web" route={webRoute()} />,
    stubClient(request),
    onUnauthorized,
  );
  return { ...result, request, onUnauthorized };
}
