/**
 * The confirm-first dialog for starting a new conversation: it clears the
 * conversation on screen and rotates the session, which cannot be undone.
 *
 * The page is a standalone document with no Studio shell around it, so the dialog is
 * composed from the SDK primitives that need no shell context (`Dialog`, `Button`,
 * `Spinner`, `ErrorState`). The SDK's `ConfirmDialog` reads a failure through the
 * shell's API client (`useApi`), which this page does not have.
 */
import { Button, Dialog, errorMessage, ErrorState, Spinner } from '@tai42/studio-sdk';
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
    <Dialog
      title="Start a new conversation?"
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      This clears the conversation on screen and starts a fresh one. You will not be able to come
      back to this one.
      {error !== null ? <ErrorState message={errorMessage(error)} /> : null}
      <div className="tai-dialog-actions">
        <Button type="button" onClick={onClose}>
          Cancel
        </Button>
        <Button type="button" variant="primary" disabled={isPending} onClick={onConfirm}>
          {isPending ? <Spinner label="Starting" /> : null}
          Start new
        </Button>
      </div>
    </Dialog>
  );
}
