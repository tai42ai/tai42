/**
 * The two unobtrusive status pills below the transcript: a spinner while a loaded
 * conversation reconnects, and a warning when a frame on an otherwise-live
 * conversation could not be read (the stream carries on, so something was lost
 * without taking the page away — the hook never drops a bad frame in silence).
 */
import type { ReactElement } from 'react';
import { Spinner } from '@tai42/studio-sdk';

export function ConnectionStatus({
  reconnecting,
  frameDropped,
}: {
  readonly reconnecting: boolean;
  readonly frameDropped: boolean;
}): ReactElement {
  return (
    <>
      {reconnecting ? (
        <p className="tcw-pill" role="status">
          <Spinner label="Reconnecting" />
          <span>Reconnecting…</span>
        </p>
      ) : null}
      {frameDropped ? (
        <p className="tcw-pill tcw-pill--warn" role="status">
          <span>Part of this conversation couldn&apos;t be shown.</span>
        </p>
      ) : null}
    </>
  );
}
