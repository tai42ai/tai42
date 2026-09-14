/**
 * Regenerate a pending user's invite, replacing the live one, and show the new
 * one-time link.
 */
import type { ReactElement } from 'react';
import { useCallback, useState } from 'react';
import { Button, Dialog, ErrorState, Spinner, errorMessage } from '@tai42/studio-sdk';
import type { AdminUser, InviteResult, UsersAdminApi } from '@/api';
import { InviteResultView } from '@/invite-result-view';

export function RegenerateInviteDialog({
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
