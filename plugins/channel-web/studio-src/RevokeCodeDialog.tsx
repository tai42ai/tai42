/**
 * The revoke-entry-code confirm. Revoking is immediate and irreversible — the code's
 * chat link stops working at once, and a replacement means revoke-then-mint.
 */
import { Button, Dialog, errorMessage, ErrorState, Spinner } from '@tai42/studio-sdk';
import type { ReactNode } from 'react';

export function RevokeCodeDialog({
  isError,
  error,
  isPending,
  onCancel,
  onConfirm,
}: {
  readonly isError: boolean;
  readonly error: unknown;
  readonly isPending: boolean;
  readonly onCancel: () => void;
  readonly onConfirm: () => void;
}): ReactNode {
  return (
    <Dialog
      open
      title="Revoke entry code"
      description="Revoke this code? Its chat link stops working immediately and cannot be restored — a new code means revoke and mint."
      onOpenChange={(next) => {
        if (!next) onCancel();
      }}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--tai-space-4)' }}>
        {isError ? <ErrorState message={errorMessage(error)} /> : null}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--tai-space-3)' }}>
          <Button onClick={onCancel} disabled={isPending}>
            Cancel
          </Button>
          <Button variant="danger" onClick={onConfirm} disabled={isPending}>
            {isPending ? <Spinner label="Revoking" /> : null}
            Revoke code
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
