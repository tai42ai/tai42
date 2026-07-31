/**
 * The users-admin page: list the deployment's accounts, invite new users, change a
 * user's role, disable/enable, regenerate a pending invite, and delete. Every call
 * hits this plugin's own `/api/auth/users*` routes through {@link useUsersAdminApi}
 * (which carries the host session credential); the role picker's options come from
 * `GET /api/auth/roles`, never a hardcoded list, so an operator-authored role
 * template appears here without a plugin change.
 *
 * The whole surface is built from `@tai42/studio-sdk` design-system components, so it
 * themes with the shell and stays inside the plugin styling contract.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactElement } from 'react';
import {
  Badge,
  Button,
  ConfirmDialog,
  CopyField,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Select,
  Spinner,
  TBody,
  TD,
  TH,
  THead,
  TR,
  Table,
  TextInput,
  errorMessage,
} from '@tai42/studio-sdk';
import type { PluginPageProps } from '@tai42/studio-sdk';
import { useUsersAdminApi } from '@/api';
import type { AdminUser, InviteResult, RoleTemplate, UsersAdminApi } from '@/api';

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

/** The one-time invite link, shown once with its shown-once warning. Shared by the
 * create flow and invite regeneration. */
function InviteResultView({ result }: { result: InviteResult }): ReactElement {
  return (
    <div className="users-dialog-body">
      <CopyField
        label="Invite link"
        value={result.login_path}
        caption="Copy this link now — it is shown only once. Send it to the user; opening it lets them set a password and sign in."
      />
    </div>
  );
}

/**
 * A confirm-then-run modal for a destructive/administrative row action. Owns its
 * pending + error state so a backend refusal (e.g. the last-enabled-admin guard's
 * 409) surfaces loudly in place; only a success closes and reloads the list.
 */
function ActionConfirm({
  title,
  confirmLabel,
  pendingLabel,
  confirmVariant,
  run,
  onClose,
  onDone,
  children,
}: {
  title: string;
  confirmLabel: string;
  pendingLabel: string;
  confirmVariant?: 'primary' | 'danger';
  run: () => Promise<unknown>;
  onClose: () => void;
  onDone: () => void;
  children: ReactElement | string;
}): ReactElement {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const confirm = useCallback(() => {
    setPending(true);
    setError(null);
    run().then(
      () => {
        onDone();
      },
      (err: unknown) => {
        setError(err instanceof Error ? err : new Error(String(err)));
        setPending(false);
      },
    );
  }, [run, onDone]);

  return (
    <ConfirmDialog
      title={title}
      confirmLabel={confirmLabel}
      pendingLabel={pendingLabel}
      confirmVariant={confirmVariant}
      isPending={pending}
      error={error}
      onConfirm={confirm}
      onClose={onClose}
    >
      {children}
    </ConfirmDialog>
  );
}

/** Invite a new user: email + role, then a one-time invite link. On success the
 * list reloads behind the dialog so the new pending user is already visible. */
function CreateUserDialog({
  roles,
  api,
  onClose,
  onCreated,
}: {
  roles: readonly RoleTemplate[];
  api: UsersAdminApi;
  onClose: () => void;
  onCreated: () => void;
}): ReactElement {
  const [email, setEmail] = useState('');
  const [role, setRole] = useState(roles[0]?.name ?? '');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<InviteResult | null>(null);

  const options = useMemo(() => roles.map((r) => ({ value: r.name, label: r.name })), [roles]);
  const canSubmit = email.trim().length > 0 && role.length > 0 && !pending;

  const submit = useCallback(() => {
    setPending(true);
    setError(null);
    api.createUser({ email: email.trim(), role }).then(
      (created) => {
        setResult(created);
        setPending(false);
        onCreated();
      },
      (err: unknown) => {
        setError(errorMessage(err));
        setPending(false);
      },
    );
  }, [api, email, role, onCreated]);

  if (result !== null) {
    return (
      <Dialog
        title="Invite created"
        open
        onOpenChange={(next) => {
          if (!next) onClose();
        }}
      >
        <InviteResultView result={result} />
        <div className="users-dialog-actions" style={{ marginTop: 'var(--tai-space-4)' }}>
          <Button type="button" variant="primary" onClick={onClose}>
            Done
          </Button>
        </div>
      </Dialog>
    );
  }

  return (
    <Dialog
      title="Invite user"
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <div className="users-dialog-body">
        <Field label="Email">
          <TextInput
            type="email"
            aria-label="Email"
            value={email}
            onChange={(event) => {
              setEmail(event.target.value);
            }}
          />
        </Field>
        <Field label="Role">
          <Select
            aria-label="Role"
            options={options}
            value={role}
            onValueChange={setRole}
            placeholder={options.length === 0 ? 'No roles available' : 'Select a role'}
            disabled={options.length === 0}
          />
        </Field>
        {error !== null ? <ErrorState message={error} /> : null}
        <div className="users-dialog-actions">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="button" variant="primary" disabled={!canSubmit} onClick={submit}>
            {pending ? <Spinner label="Creating invite" /> : null}
            Send invite
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

/** Change a user's role from the seeded role templates. */
function RoleDialog({
  user,
  roles,
  api,
  onClose,
  onDone,
}: {
  user: AdminUser;
  roles: readonly RoleTemplate[];
  api: UsersAdminApi;
  onClose: () => void;
  onDone: () => void;
}): ReactElement {
  const [role, setRole] = useState(user.role);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const options = useMemo(() => roles.map((r) => ({ value: r.name, label: r.name })), [roles]);
  const canSubmit = role !== user.role && !pending;

  const submit = useCallback(() => {
    setPending(true);
    setError(null);
    api.setRole(user.user_id, role).then(
      () => {
        onDone();
      },
      (err: unknown) => {
        setError(errorMessage(err));
        setPending(false);
      },
    );
  }, [api, user.user_id, role, onDone]);

  return (
    <Dialog
      title={`Change role — ${user.email}`}
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <div className="users-dialog-body">
        <Field label="Role">
          <Select aria-label="Role" options={options} value={role} onValueChange={setRole} />
        </Field>
        {error !== null ? <ErrorState message={error} /> : null}
        <div className="users-dialog-actions">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="button" variant="primary" disabled={!canSubmit} onClick={submit}>
            {pending ? <Spinner label="Saving role" /> : null}
            Save
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

/** Regenerate a pending user's invite, replacing the live one, and show the new
 * one-time link. */
function RegenerateInviteDialog({
  user,
  api,
  onClose,
  onDone,
}: {
  user: AdminUser;
  api: UsersAdminApi;
  onClose: () => void;
  onDone: () => void;
}): ReactElement {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<InviteResult | null>(null);

  const regenerate = useCallback(() => {
    setPending(true);
    setError(null);
    api.regenerateInvite(user.user_id).then(
      (fresh) => {
        setResult(fresh);
        setPending(false);
        onDone();
      },
      (err: unknown) => {
        setError(errorMessage(err));
        setPending(false);
      },
    );
  }, [api, user.user_id, onDone]);

  if (result !== null) {
    return (
      <Dialog
        title="Invite regenerated"
        open
        onOpenChange={(next) => {
          if (!next) onClose();
        }}
      >
        <InviteResultView result={result} />
        <div className="users-dialog-actions" style={{ marginTop: 'var(--tai-space-4)' }}>
          <Button type="button" variant="primary" onClick={onClose}>
            Done
          </Button>
        </div>
      </Dialog>
    );
  }

  return (
    <Dialog
      title={`Regenerate invite — ${user.email}`}
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <div className="users-dialog-body">
        <p style={{ margin: 0 }}>
          This replaces the current invite link. The old link stops working immediately.
        </p>
        {error !== null ? <ErrorState message={error} /> : null}
        <div className="users-dialog-actions">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="button" variant="primary" disabled={pending} onClick={regenerate}>
            {pending ? <Spinner label="Regenerating invite" /> : null}
            Regenerate
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

type RowAction =
  | { readonly kind: 'role'; readonly user: AdminUser }
  | { readonly kind: 'disable'; readonly user: AdminUser }
  | { readonly kind: 'invite'; readonly user: AdminUser }
  | { readonly kind: 'delete'; readonly user: AdminUser };

/** The per-row action buttons. Regenerate-invite shows only while the user's
 * invite is still pending (a set password 409s that route). */
function RowActions({
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

export function UsersPage(_props: PluginPageProps): ReactElement {
  const api = useUsersAdminApi();

  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [roles, setRoles] = useState<RoleTemplate[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);

  const [createOpen, setCreateOpen] = useState(false);
  const [action, setAction] = useState<RowAction | null>(null);

  const reload = useCallback(() => {
    setVersion((v) => v + 1);
  }, []);
  const closeAction = useCallback(() => {
    setAction(null);
  }, []);
  const finishAction = useCallback(() => {
    setAction(null);
    reload();
  }, [reload]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    Promise.all([api.listUsers(controller.signal), api.listRoles(controller.signal)]).then(
      ([nextUsers, nextRoles]) => {
        if (controller.signal.aborted) return;
        setUsers(nextUsers);
        setRoles(nextRoles);
        setLoading(false);
      },
      (err: unknown) => {
        if (controller.signal.aborted) return;
        setLoadError(errorMessage(err));
        setLoading(false);
      },
    );
    return () => {
      controller.abort();
    };
  }, [api, version]);

  const body = ((): ReactElement => {
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
                <RowActions user={user} onAction={setAction} />
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
    );
  })();

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

        {body}

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

        {action?.kind === 'role' ? (
          <RoleDialog
            user={action.user}
            roles={roles}
            api={api}
            onClose={closeAction}
            onDone={finishAction}
          />
        ) : null}

        {action?.kind === 'disable' ? (
          <ActionConfirm
            title={action.user.disabled ? 'Enable user' : 'Disable user'}
            confirmLabel={action.user.disabled ? 'Enable' : 'Disable'}
            pendingLabel={action.user.disabled ? 'Enabling' : 'Disabling'}
            confirmVariant={action.user.disabled ? 'primary' : 'danger'}
            run={() => api.setDisabled(action.user.user_id, !action.user.disabled)}
            onClose={closeAction}
            onDone={finishAction}
          >
            {action.user.disabled
              ? `Re-enable ${action.user.email}? Their sessions were revoked when they were disabled and must sign in again.`
              : `Disable ${action.user.email}? This revokes their sessions and API keys immediately.`}
          </ActionConfirm>
        ) : null}

        {action?.kind === 'invite' ? (
          <RegenerateInviteDialog
            user={action.user}
            api={api}
            onClose={closeAction}
            onDone={reload}
          />
        ) : null}

        {action?.kind === 'delete' ? (
          <ActionConfirm
            title="Delete user"
            confirmLabel="Delete"
            pendingLabel="Deleting"
            run={() => api.deleteUser(action.user.user_id)}
            onClose={closeAction}
            onDone={finishAction}
          >
            {`Delete ${action.user.email}? This removes their account, sessions, invites, and access-control policy. This cannot be undone.`}
          </ActionConfirm>
        ) : null}
      </div>
    </div>
  );
}
