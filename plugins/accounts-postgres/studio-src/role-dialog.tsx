/** Change a user's role from the seeded role templates. */
import type { ReactElement } from 'react';
import { useCallback, useMemo, useState } from 'react';
import {
  Button,
  Dialog,
  ErrorState,
  Field,
  Select,
  Spinner,
  errorMessage,
} from '@tai42/studio-sdk';
import type { AdminUser, RoleTemplate, UsersAdminApi } from '@/api';

export function RoleDialog({
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
