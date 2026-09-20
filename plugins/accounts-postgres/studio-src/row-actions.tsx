/**
 * The per-row action buttons and the action they raise. Regenerate-invite shows
 * only while the user's invite is still pending (a set password 409s that route).
 */
import { Button } from '@tai42/studio-sdk';
import type { ReactElement } from 'react';

import type { AdminUser } from '@/api';

export type RowAction =
  | { readonly kind: 'role'; readonly user: AdminUser }
  | { readonly kind: 'disable'; readonly user: AdminUser }
  | { readonly kind: 'invite'; readonly user: AdminUser }
  | { readonly kind: 'delete'; readonly user: AdminUser };

export function RowActions({
  user,
  onAction,
}: {
  user: AdminUser;
  onAction: (action: RowAction) => void;
}): ReactElement {
  return (
    <div className="users-row-actions">
      <Button type="button" onClick={() => onAction({ kind: 'role', user })}>
        Change role
      </Button>
      <Button type="button" onClick={() => onAction({ kind: 'disable', user })}>
        {user.disabled ? 'Enable' : 'Disable'}
      </Button>
      {user.pending_invite ? (
        <Button type="button" onClick={() => onAction({ kind: 'invite', user })}>
          Regenerate invite
        </Button>
      ) : null}
      <Button type="button" variant="danger" onClick={() => onAction({ kind: 'delete', user })}>
        Delete
      </Button>
    </div>
  );
}
