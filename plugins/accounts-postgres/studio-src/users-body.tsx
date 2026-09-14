/**
 * The users-admin table body: a spinner while the first load runs, a loud error
 * with a retry when it fails, an empty state when there are no users, else the
 * table of accounts with a status badge and per-row actions.
 */
import type { ReactElement } from 'react';
import {
  Badge,
  EmptyState,
  ErrorState,
  Spinner,
  TBody,
  TD,
  TH,
  THead,
  TR,
  Table,
} from '@tai42/studio-sdk';
import type { AdminUser } from '@/api';
import { RowActions, type RowAction } from '@/row-actions';

/** A user's live state, collapsed to one badge. A pending invite (no password set
 * yet) takes precedence over the enabled/disabled distinction. */
function StatusBadge({ user }: { user: AdminUser }): ReactElement {
  if (user.pending_invite) return <Badge variant="warning">Invite pending</Badge>;
  if (user.disabled) return <Badge variant="danger">Disabled</Badge>;
  return <Badge variant="success">Active</Badge>;
}

/** Render an ISO timestamp as a plain local date; fall back to the raw string if
 * it does not parse (surfaced, never blank). */
function formatCreated(iso: string): string {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleDateString();
}

export function UsersBody({
  loading,
  users,
  loadError,
  reload,
  onAction,
}: {
  loading: boolean;
  users: AdminUser[] | null;
  loadError: string | null;
  reload: () => void;
  onAction: (action: RowAction) => void;
}): ReactElement {
  if (loading && users === null) return <Spinner label="Loading users" />;
  if (loadError !== null && users === null) {
    return <ErrorState message={loadError} onRetry={reload} />;
  }
  if (users !== null && users.length === 0) {
    return (
      <EmptyState
        title="No users yet"
        description="Invite the first user to get them a one-time sign-in link."
      />
    );
  }
  return (
    <Table>
      <THead>
        <TR>
          <TH>Email</TH>
          <TH>Role</TH>
          <TH>Status</TH>
          <TH>Created</TH>
          <TH>
            <span className="users-cell-muted">Actions</span>
          </TH>
        </TR>
      </THead>
      <TBody>
        {(users ?? []).map((user) => (
          <TR key={user.user_id}>
            <TD>{user.email}</TD>
            <TD>
              <Badge variant="primary">{user.role}</Badge>
            </TD>
            <TD>
              <StatusBadge user={user} />
            </TD>
            <TD>
              <span className="users-cell-muted">{formatCreated(user.created_at)}</span>
            </TD>
            <TD>
              <RowActions user={user} onAction={onAction} />
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}
