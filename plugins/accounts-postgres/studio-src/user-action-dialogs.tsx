/**
 * The dialog for the row action in flight: change role, enable/disable, regenerate
 * an invite, or delete. Role and the destructive confirms close-and-reload on
 * success; regenerate reloads behind itself and stays open to show the new link.
 */
import type { ReactElement } from 'react';

import { ActionConfirm } from '@/action-confirm';
import type { RoleTemplate, UsersAdminApi } from '@/api';
import { RegenerateInviteDialog } from '@/regenerate-invite-dialog';
import { RoleDialog } from '@/role-dialog';
import type { RowAction } from '@/row-actions';

export function UserActionDialogs({
  action,
  roles,
  api,
  onClose,
  onFinish,
  onReload,
}: {
  action: RowAction | null;
  roles: readonly RoleTemplate[];
  api: UsersAdminApi;
  onClose: () => void;
  onFinish: () => void;
  onReload: () => void;
}): ReactElement | null {
  if (action === null) return null;
  if (action.kind === 'role') {
    return (
      <RoleDialog user={action.user} roles={roles} api={api} onClose={onClose} onDone={onFinish} />
    );
  }
  if (action.kind === 'disable') {
    return (
      <ActionConfirm
        title={action.user.disabled ? 'Enable user' : 'Disable user'}
        confirmLabel={action.user.disabled ? 'Enable' : 'Disable'}
        pendingLabel={action.user.disabled ? 'Enabling' : 'Disabling'}
        confirmVariant={action.user.disabled ? 'primary' : 'danger'}
        run={() => api.setDisabled(action.user.user_id, !action.user.disabled)}
        onClose={onClose}
        onDone={onFinish}
      >
        {action.user.disabled
          ? `Re-enable ${action.user.email}? Their sessions were revoked when they were disabled and must sign in again.`
          : `Disable ${action.user.email}? This revokes their sessions and API keys immediately.`}
      </ActionConfirm>
    );
  }
  if (action.kind === 'invite') {
    return (
      <RegenerateInviteDialog user={action.user} api={api} onClose={onClose} onDone={onReload} />
    );
  }
  return (
    <ActionConfirm
      title="Delete user"
      confirmLabel="Delete"
      pendingLabel="Deleting"
      run={() => api.deleteUser(action.user.user_id)}
      onClose={onClose}
      onDone={onFinish}
    >
      {`Delete ${action.user.email}? This removes their account, sessions, invites, and access-control policy. This cannot be undone.`}
    </ActionConfirm>
  );
}
