/**
 * The "new conversation" lifecycle: the confirm dialog, the session rotation, and
 * the epoch/generation counters that keep a rotate clean.
 *
 * `generationRef` is bumped the moment a rotate succeeds — before any render — so a
 * send that was already in flight can tell that its answer belongs to the
 * conversation the visitor has just left. `epoch` restarts the transcript stream
 * from empty. On a successful rotate `resetLocalStateRef` clears the page's own
 * local state (the outbox, the draft, the typing bubble) synchronously, so the old
 * conversation's bubbles cannot flash into the fresh one.
 */
import type { RefObject } from 'react';
import { useCallback, useRef, useState } from 'react';

import { rotateSession } from '@/api';

export interface ConversationReset {
  /** Restarts the transcript stream from empty; also the `Transcript` key. */
  readonly epoch: number;
  /** Which conversation the page is on — bumped on a successful rotate. */
  readonly generationRef: RefObject<number>;
  /** A send/answer/submit reported the session gone. */
  readonly sessionEnded: boolean;
  /** End the conversation (a door reported the session gone). */
  readonly endConversation: () => void;
  /** Re-open the stream without rotating (the reachable-error retry). */
  readonly restartStream: () => void;
  readonly confirmingReset: boolean;
  readonly openReset: () => void;
  readonly closeReset: () => void;
  readonly resetting: boolean;
  readonly resetError: Error | null;
  readonly confirmReset: () => void;
}

export function useConversationReset(params: {
  identity: string;
  entryCode: string | null;
  resetLocalStateRef: RefObject<() => void>;
}): ConversationReset {
  const { identity, entryCode, resetLocalStateRef } = params;
  const [epoch, setEpoch] = useState(0);
  const [sessionEnded, setSessionEnded] = useState(false);
  const [confirmingReset, setConfirmingReset] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState<Error | null>(null);
  const generationRef = useRef(0);

  const endConversation = useCallback(() => {
    setSessionEnded(true);
  }, []);
  const restartStream = useCallback(() => {
    setEpoch((current) => current + 1);
  }, []);
  const openReset = useCallback(() => {
    setConfirmingReset(true);
  }, []);
  const closeReset = useCallback(() => {
    setConfirmingReset(false);
    setResetError(null);
  }, []);

  const confirmReset = useCallback(() => {
    setResetting(true);
    setResetError(null);
    rotateSession(identity, entryCode).then(
      () => {
        generationRef.current += 1;
        setResetting(false);
        setConfirmingReset(false);
        resetLocalStateRef.current();
        setSessionEnded(false);
        // A new address means a new (empty) transcript: restart the stream so the
        // old conversation's items cannot survive into the new one.
        setEpoch((current) => current + 1);
      },
      (err: unknown) => {
        setResetting(false);
        setResetError(err instanceof Error ? err : new Error(String(err)));
      },
    );
  }, [identity, entryCode, resetLocalStateRef]);

  return {
    epoch,
    generationRef,
    sessionEnded,
    endConversation,
    restartStream,
    confirmingReset,
    openReset,
    closeReset,
    resetting,
    resetError,
    confirmReset,
  };
}
