/**
 * The confirm-first dialog for starting a new conversation: it clears the
 * conversation on screen and rotates the session, which cannot be undone.
 */
import { ConfirmDialog } from '@tai42/studio-sdk';
import type { ReactElement } from 'react';

export function ResetConfirmDialog({
  isPending,
  error,
  onConfirm,
  onClose,
}: {
  readonly isPending: boolean;
  readonly error: Error | null;
  readonly onConfirm: () => void;
  readonly onClose: () => void;
}): ReactElement {
  return (
    <ConfirmDialog
      title="Start a new conversation?"
      confirmLabel="Start new"
      pendingLabel="Starting"
      confirmVariant="primary"
      isPending={isPending}
      error={error}
      onConfirm={onConfirm}
      onClose={onClose}
    >
      This clears the conversation on screen and starts a fresh one. You will not be able to come
      back to this one.
    </ConfirmDialog>
  );
}
