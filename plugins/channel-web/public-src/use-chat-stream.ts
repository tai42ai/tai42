/**
 * The React binding for one visitor's transcript: it drives {@link streamTranscript}
 * for as long as the page is mounted, folds each frame through {@link applyFrame},
 * and publishes the live state the page renders.
 *
 * A malformed frame is SURFACED as an error, never rendered as a blank bubble and
 * never dropped in silence; one good frame afterwards clears it, because a single
 * bad entry on a healthy stream must not stick.
 */
import { useEffect, useRef, useState } from 'react';

import { applyFrame } from '@/frame-reducer';
import { type ChatItem, EMPTY_MODEL, type StreamModel } from '@/transcript-model';
import { streamTranscript, type TranscriptSink } from '@/transcript-subscription';

/** The live state of one visitor's transcript. */
export interface ChatStreamState {
  readonly items: readonly ChatItem[];
  readonly answeredIds: ReadonlySet<string>;
  readonly connected: boolean;
  /** The replayed backlog has arrived at least once — until then the page shows
   * its loading state rather than an empty conversation. */
  readonly backlogLoaded: boolean;
  readonly error: Error | null;
  /** Terminal: the deployment runs no transcript store, so reconnecting would
   * replay the same 501 forever. */
  readonly disabled: boolean;
  /** Terminal: the session cookie resolves to nothing; only a reload recovers. */
  readonly sessionExpired: boolean;
}

const INITIAL_STATE: ChatStreamState = {
  items: EMPTY_MODEL.items,
  answeredIds: EMPTY_MODEL.answeredIds,
  connected: false,
  backlogLoaded: false,
  error: null,
  disabled: false,
  sessionExpired: false,
};

/**
 * Subscribe to the visitor's transcript for as long as the page is mounted.
 *
 * `epoch` restarts the subscription from an EMPTY model: rotating the session
 * gives the visitor a new address, so the old conversation's items must not
 * survive into the new one.
 */
export function useChatStream(identity: string, epoch: number): ChatStreamState {
  const [state, setState] = useState<ChatStreamState>(INITIAL_STATE);
  const modelRef = useRef<StreamModel>(EMPTY_MODEL);

  useEffect(() => {
    const controller = new AbortController();
    modelRef.current = EMPTY_MODEL;
    setState(INITIAL_STATE);

    const sink: TranscriptSink = {
      onConnected() {
        setState((prev) => ({ ...prev, connected: true, error: null }));
      },
      onFrame(frame) {
        const outcome = applyFrame(modelRef.current, frame);
        if (outcome.kind === 'malformed') {
          setState((prev) => ({
            ...prev,
            error: new Error(`malformed transcript frame: ${outcome.event}`),
          }));
          return;
        }
        if (outcome.kind === 'backlog-done') {
          setState((prev) => ({ ...prev, backlogLoaded: true, error: null }));
          return;
        }
        modelRef.current = outcome.model;
        setState((prev) => ({
          ...prev,
          items: outcome.model.items,
          answeredIds: outcome.model.answeredIds,
          error: null,
        }));
      },
      onDisconnected() {
        setState((prev) => ({ ...prev, connected: false }));
      },
      onError(error) {
        setState((prev) => ({ ...prev, connected: false, error }));
      },
      onSessionExpired(error) {
        setState((prev) => ({ ...prev, connected: false, sessionExpired: true, error }));
      },
      onStoreOff(error) {
        setState((prev) => ({ ...prev, connected: false, disabled: true, error }));
      },
    };

    void streamTranscript(identity, controller.signal, sink);
    return () => {
      controller.abort();
    };
  }, [identity, epoch]);

  return state;
}
