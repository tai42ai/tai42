/**
 * The users-admin page: list the deployment's accounts, invite new users, change a
 * user's role, disable/enable, regenerate a pending invite, and delete. The list
 * and role templates come from {@link useUsersAdmin} (which carries the host session
 * credential and reads `GET /api/auth/roles`, never a hardcoded list, so an
 * operator-authored role template appears here without a plugin change).
 *
 * The whole surface is built from `@tai42/studio-sdk` design-system components, so it
 * themes with the shell and stays inside the plugin styling contract.
 */
import type { ReactElement } from 'react';
import { useCallback, useState } from 'react';
import { Button } from '@tai42/studio-sdk';
import type { PluginPageProps } from '@tai42/studio-sdk';
import { useUsersAdmin } from '@/use-users-admin';
import { UsersBody } from '@/users-body';
import { CreateUserDialog } from '@/create-user-dialog';
import { UserActionDialogs } from '@/user-action-dialogs';
import type { RowAction } from '@/row-actions';

export function UsersPage(_props: PluginPageProps): ReactElement {
  const { api, users, roles, loadError, loading, reload } = useUsersAdmin();

  const [createOpen, setCreateOpen] = useState(false);
  const [action, setAction] = useState<RowAction | null>(null);

  const closeAction = useCallback(() => {
    setAction(null);
  }, []);
  const finishAction = useCallback(() => {
    setAction(null);
    reload();
  }, [reload]);

  return (
    <div className="tai42_accounts_postgres-root">
      <div className="users-page">
        <div className="users-toolbar">
          <h1 className="users-toolbar-title">Users</h1>
          <Button
            type="button"
            variant="primary"
            onClick={() => {
              setCreateOpen(true);
            }}
          >
            Invite user
          </Button>
        </div>

        <UsersBody
          loading={loading}
          users={users}
          loadError={loadError}
          reload={reload}
          onAction={setAction}
        />

        {createOpen ? (
          <CreateUserDialog
            roles={roles}
            api={api}
            onClose={() => {
              setCreateOpen(false);
            }}
            onCreated={reload}
          />
        ) : null}

        <UserActionDialogs
          action={action}
          roles={roles}
          api={api}
          onClose={closeAction}
          onFinish={finishAction}
          onReload={reload}
        />
      </div>
    </div>
  );
}
