import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { AdminUser, InviteResult, RoleTemplate, UsersAdminApi } from '@/api';
import { UsersPage } from './users-page';

// The page reaches the backend only through `useUsersAdminApi`; the tests drive a
// recording fake in its place, so no network, credential, or provider is needed.
const { apiHolder } = vi.hoisted(() => ({
  apiHolder: { current: null as UsersAdminApi | null },
}));
vi.mock('@/api', () => ({
  useUsersAdminApi: (): UsersAdminApi => {
    if (apiHolder.current === null) throw new Error('test api not set');
    return apiHolder.current;
  },
}));

const USERS: AdminUser[] = [
  {
    user_id: 'usr-alice',
    email: 'alice@example.com',
    role: 'admin',
    disabled: false,
    created_at: '2026-01-01T00:00:00Z',
    pending_invite: false,
  },
  {
    user_id: 'usr-bob',
    email: 'bob@example.com',
    role: 'editor',
    disabled: false,
    created_at: '2026-02-01T00:00:00Z',
    pending_invite: true,
  },
];

const ROLES: RoleTemplate[] = [
  { name: 'admin', scopes: ['*'], condition: null, description: 'Owner' },
  { name: 'editor', scopes: ['*'], condition: 'editor-jq', description: 'Editor' },
  { name: 'viewer', scopes: ['*'], condition: 'viewer-jq', description: 'Viewer' },
];

const INVITE: InviteResult = {
  user_id: 'usr-carol',
  invite_token: 'tai-inv-secret',
  login_path: '/login?invite=tai-inv-secret',
};

function makeApi(overrides: Partial<UsersAdminApi> = {}): UsersAdminApi {
  return {
    listUsers: vi.fn().mockResolvedValue(USERS),
    listRoles: vi.fn().mockResolvedValue(ROLES),
    createUser: vi.fn().mockResolvedValue(INVITE),
    setRole: vi.fn().mockResolvedValue(undefined),
    setDisabled: vi.fn().mockResolvedValue(undefined),
    deleteUser: vi.fn().mockResolvedValue(undefined),
    regenerateInvite: vi.fn().mockResolvedValue(INVITE),
    ...overrides,
  };
}

beforeEach(() => {
  apiHolder.current = makeApi();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('UsersPage', () => {
  it('lists users with their role and status', async () => {
    render(<UsersPage pluginId="tai42_accounts_postgres" />);

    expect(await screen.findByText('alice@example.com')).toBeInTheDocument();
    expect(screen.getByText('bob@example.com')).toBeInTheDocument();
    // Bob has a pending invite; Alice is active.
    expect(screen.getByText('Invite pending')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
  });

  it('invites a user and shows the one-time login link', async () => {
    const user = userEvent.setup();
    render(<UsersPage pluginId="tai42_accounts_postgres" />);
    await screen.findByText('alice@example.com');

    await user.click(screen.getByRole('button', { name: 'Invite user' }));

    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText('Email'), 'carol@example.com');
    await user.click(within(dialog).getByRole('button', { name: 'Send invite' }));

    // The create call fired with the typed email and the default (first) role.
    await waitFor(() => {
      expect(apiHolder.current?.createUser).toHaveBeenCalledWith({
        email: 'carol@example.com',
        role: 'admin',
      });
    });
    // The one-time login link is shown for the admin to copy.
    expect(await screen.findByText('/login?invite=tai-inv-secret')).toBeInTheDocument();
  });

  it('surfaces the last-admin guard error when a delete is refused', async () => {
    apiHolder.current = makeApi({
      deleteUser: vi.fn().mockRejectedValue(new Error('Cannot delete the last enabled admin')),
    });
    const user = userEvent.setup();
    render(<UsersPage pluginId="tai42_accounts_postgres" />);
    await screen.findByText('alice@example.com');

    // Open the confirm for Alice (the first row) and confirm the delete.
    await user.click(screen.getAllByRole('button', { name: 'Delete' })[0]!);
    const dialog = screen.getByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }));

    expect(await screen.findByText('Cannot delete the last enabled admin')).toBeInTheDocument();
    // The refusal keeps the dialog open — nothing was silently dropped.
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('disables a user through the confirm dialog', async () => {
    const user = userEvent.setup();
    render(<UsersPage pluginId="tai42_accounts_postgres" />);
    await screen.findByText('alice@example.com');

    // Alice is the first active row; open her disable confirm and confirm it.
    await user.click(screen.getAllByRole('button', { name: 'Disable' })[0]!);
    const dialog = screen.getByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Disable' }));

    await waitFor(() => {
      expect(apiHolder.current?.setDisabled).toHaveBeenCalledWith('usr-alice', true);
    });
    // A success closes the dialog and reloads the list.
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
  });
});
