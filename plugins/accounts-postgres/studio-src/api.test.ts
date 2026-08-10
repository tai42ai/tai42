import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { createElement } from 'react';
import type { ReactNode } from 'react';
import { AuthProvider, UnauthorizedProvider } from '@tai42/studio-sdk';
import type { AdminUser, InviteResult } from './api';
import { useUsersAdminApi } from './api';

// The api seam is bound to the live host credential (`useAuth`) and the shell's
// 401 handler (`useOnUnauthorized`). These tests drive it through a stubbed
// `fetch`, seeding the credential via the SDK's own AuthProvider (which reads
// sessionStorage) and supplying the unauthorized handler through its provider.

const TOKEN = 'sk-live-credential';
const SESSION_KEY = 'tai-studio.apiKey';

const USER: AdminUser = {
  user_id: 'usr-alice',
  email: 'alice@example.com',
  role: 'admin',
  disabled: false,
  created_at: '2026-01-01T00:00:00Z',
  pending_invite: false,
};

const INVITE: InviteResult = {
  user_id: 'usr-carol',
  invite_token: 'tai-inv-secret',
  login_path: '/login?invite=tai-inv-secret',
};

function makeWrapper(onUnauthorized: () => void) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(
      AuthProvider,
      null,
      createElement(UnauthorizedProvider, { value: onUnauthorized }, children),
    );
  };
}

function fakeResponse(status: number, body: unknown): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: async () => JSON.stringify(body),
  } as Response;
}

function stubFetch(impl: (...args: unknown[]) => Promise<Response>) {
  const fn = vi.fn(impl);
  vi.stubGlobal('fetch', fn);
  return fn;
}

function renderApi(onUnauthorized: () => void = vi.fn()) {
  const { result } = renderHook(() => useUsersAdminApi(), { wrapper: makeWrapper(onUnauthorized) });
  return result;
}

beforeEach(() => {
  globalThis.sessionStorage.setItem(SESSION_KEY, TOKEN);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  globalThis.sessionStorage.clear();
});

describe('useUsersAdminApi request seam', () => {
  it('carries the credential as the x-api-key header', async () => {
    const fetchFn = stubFetch(async () => fakeResponse(200, { data: { users: [] } }));
    const api = renderApi();

    await api.current.listUsers();

    const [, init] = fetchFn.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get('x-api-key')).toBe(TOKEN);
    expect(headers.get('accept')).toBe('application/json');
  });

  it('unwraps a {data} envelope, and listUsers to the nested array', async () => {
    stubFetch(async () => fakeResponse(200, { data: { users: [USER] } }));
    const users = await renderApi().current.listUsers();
    expect(users).toEqual([USER]);
    expect(Array.isArray(users)).toBe(true);
  });

  it('unwraps a flat {data} object for createUser', async () => {
    stubFetch(async () => fakeResponse(200, { data: INVITE }));
    const invite = await renderApi().current.createUser({ email: 'c@x.y', role: 'admin' });
    expect(invite).toEqual(INVITE);
  });

  it('throws the backend error message verbatim on a non-2xx {error} body', async () => {
    stubFetch(async () => fakeResponse(409, { error: 'cannot delete the last enabled admin' }));
    await expect(renderApi().current.deleteUser('usr-1')).rejects.toThrow(
      'cannot delete the last enabled admin',
    );
  });

  it('routes a 401 through onUnauthorized and still rejects', async () => {
    const onUnauthorized = vi.fn();
    stubFetch(async () => fakeResponse(401, { error: 'dead credential' }));
    await expect(renderApi(onUnauthorized).current.listUsers()).rejects.toThrow();
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});
