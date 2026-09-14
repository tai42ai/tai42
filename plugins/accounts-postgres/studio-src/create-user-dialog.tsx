/**
 * Invite a new user: email + role, then a one-time invite link. On success the
 * list reloads behind the dialog so the new pending user is already visible.
 */
import type { ReactElement } from 'react';
import { useCallback, useMemo, useState } from 'react';
import {
  Button,
  Dialog,
  ErrorState,
  Field,
  Select,
  Spinner,
  TextInput,
  errorMessage,
} from '@tai42/studio-sdk';
import type { InviteResult, RoleTemplate, UsersAdminApi } from '@/api';
import { InviteResultView } from '@/invite-result-view';

export function CreateUserDialog({
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
