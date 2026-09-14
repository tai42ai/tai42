/**
 * The full-height body shown when the transcript cannot be presented: a store that
 * is switched off (terminal, no retry), or a backlog that never arrived (offered a
 * retry that re-opens the stream).
 */
import type { ReactElement } from 'react';
import { ErrorState } from '@tai42/studio-sdk';

export function BrokenBody({
  disabled,
  onRetry,
}: {
  readonly disabled: boolean;
  readonly onRetry: () => void;
}): ReactElement {
  return (
    <div className="tcw-centered tcw-centered--grow">
      <ErrorState
        message={
          disabled
            ? 'Chat is not switched on for this deployment yet.'
            : "We can't reach the conversation right now — it will reconnect on its own."
        }
        {...(disabled ? {} : { onRetry })}
      />
    </div>
  );
}
