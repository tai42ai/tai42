/**
 * The users-admin HTTP layer.
 *
 * These routes (`/api/auth/users*`, `/api/auth/roles`) are contributed by this
 * plugin's own backend and are not members of the host's shared typed client, so
 * the page reaches them directly with `fetch`, carrying the SAME in-memory
 * credential the shell holds (`useAuth`) as the `x-api-key` header and routing a
 * 401 through the shell's login handler (`useOnUnauthorized`) — the two SDK hooks
 * are the singleton bridge across the plugin boundary, so this binds the host's
 * live session, not a second copy. Responses use the platform envelope: success is
 * `{"data": …}`, failure is `{"error": …}`; a non-2xx surfaces the `error` text
 * (never swallowed) so the page can render the backend's own message (e.g. the
 * last-admin guard's 409).
 */
import { useMemo } from 'react';
import { useAuth, useOnUnauthorized } from '@tai42/studio-sdk';

/** A user row as listed by `GET /api/auth/users`. Never carries a hash or token. */
export interface AdminUser {
  readonly user_id: string;
  readonly email: string;
  readonly role: string;
  readonly disabled: boolean;
  readonly created_at: string;
  readonly pending_invite: boolean;
}

/** A role template as listed by `GET /api/auth/roles`. The picker shows `name`. */
export interface RoleTemplate {
  readonly name: string;
  readonly scopes: readonly string[];
  readonly condition: string | null;
  readonly description: string | null;
}

/** The one-time invite result of a create or invite-regenerate call. The
 * `login_path` is origin-relative by design — the plugin does not know the
 * deployment's public origin — and is shown to the admin exactly once. */
export interface InviteResult {
  readonly user_id: string;
  readonly invite_token: string;
  readonly login_path: string;
}

/** Create request body for `POST /api/auth/users`. */
export interface CreateUserBody {
  readonly email: string;
  readonly role: string;
}

/** The typed methods the users-admin page drives. */
export interface UsersAdminApi {
  listUsers(signal?: AbortSignal): Promise<AdminUser[]>;
  listRoles(signal?: AbortSignal): Promise<RoleTemplate[]>;
  createUser(body: CreateUserBody): Promise<InviteResult>;
  setRole(userId: string, role: string): Promise<void>;
  setDisabled(userId: string, disabled: boolean): Promise<void>;
  deleteUser(userId: string): Promise<void>;
  regenerateInvite(userId: string): Promise<InviteResult>;
}

interface Envelope<T> {
  readonly data?: T;
  readonly error?: string;
}

async function request<T>(
  path: string,
  token: string | null,
  onUnauthorized: () => void,
  init?: { method?: string; body?: unknown; signal?: AbortSignal },
): Promise<T> {
  const headers = new Headers({ accept: 'application/json' });
  if (token !== null) headers.set('x-api-key', token);
  if (init?.body !== undefined) headers.set('content-type', 'application/json');

  const response = await fetch(path, {
    method: init?.method ?? 'GET',
    headers,
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
    signal: init?.signal,
  });

  // A dead credential routes to the shell login exactly as the shared client's
  // 401 path does — then this call still rejects so the caller stops.
  if (response.status === 401) {
    onUnauthorized();
    throw new Error('Your session has expired — sign in again.');
  }

  const text = await response.text();
  const envelope = (text === '' ? {} : JSON.parse(text)) as Envelope<T>;

  if (!response.ok) {
    const message =
      typeof envelope.error === 'string' && envelope.error.length > 0
        ? envelope.error
        : `Request failed (${String(response.status)})`;
    throw new Error(message);
  }

  return envelope.data as T;
}

/**
 * The users-admin API bound to the live host credential. Memoized on the token and
 * the 401 handler so consumers get a stable reference across renders.
 */
export function useUsersAdminApi(): UsersAdminApi {
  const { token } = useAuth();
  const onUnauthorized = useOnUnauthorized();

  return useMemo<UsersAdminApi>(() => {
    const call = <T>(
      path: string,
      init?: { method?: string; body?: unknown; signal?: AbortSignal },
    ): Promise<T> => request<T>(path, token, onUnauthorized, init);

    return {
      listUsers: (signal) =>
        call<{ users: AdminUser[] }>('/api/auth/users', { signal }).then((r) => r.users),
      listRoles: (signal) => call<RoleTemplate[]>('/api/auth/roles', { signal }),
      createUser: (body) => call<InviteResult>('/api/auth/users', { method: 'POST', body }),
      setRole: (userId, role) =>
        call<unknown>(`/api/auth/users/${encodeURIComponent(userId)}`, {
          method: 'PUT',
          body: { role },
        }).then(() => undefined),
      setDisabled: (userId, disabled) =>
        call<unknown>(`/api/auth/users/${encodeURIComponent(userId)}`, {
          method: 'PUT',
          body: { disabled },
        }).then(() => undefined),
      deleteUser: (userId) =>
        call<unknown>(`/api/auth/users/${encodeURIComponent(userId)}`, {
          method: 'DELETE',
        }).then(() => undefined),
      regenerateInvite: (userId) =>
        call<InviteResult>(`/api/auth/users/${encodeURIComponent(userId)}/invite`, {
          method: 'POST',
        }),
    };
  }, [token, onUnauthorized]);
}
