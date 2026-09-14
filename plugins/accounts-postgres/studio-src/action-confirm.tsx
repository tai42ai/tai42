/**
 * A confirm-then-run modal for a destructive/administrative row action. Owns its
 * pending + error state so a backend refusal (e.g. the last-enabled-admin guard's
 * 409) surfaces loudly in place; only a success closes and reloads the list.
 */
import type { ReactElement } from 'react';
import { useCallback, useState } from 'react';
import { ConfirmDialog } from '@tai42/studio-sdk';

export function ActionConfirm({
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
