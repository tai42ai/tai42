/**
 * The "agent is typing" bubble follows the server's `chat.working` frames: an active
 * frame shows it for the frame's lifetime, a repeated active frame restarts that
 * lifetime, and a stop frame hides it. It also hides on anything that ends the wait —
 * a dead session, or a stream that can no longer carry the reply — and stays hidden
 * until the next frame arrives.
 */
import { useCallback, useEffect, useState } from 'react';

import type { WorkingFrame } from '@/frame-reducer';

export interface TypingIndicator {
  /** Whether the typing bubble is showing. */
  readonly typing: boolean;
  /** Hide the bubble until the next frame — a new conversation. */
  readonly clearTyping: () => void;
}

export function useTypingIndicator(params: {
  working: WorkingFrame | null;
  ended: boolean;
  streamHealthy: boolean;
}): TypingIndicator {
  const { working, ended, streamHealthy } = params;
  // The active frame the bubble is showing for. Each frame is its own object, so a
  // repeated assert re-runs the effect below and restarts the lifetime.
  const [shown, setShown] = useState<WorkingFrame | null>(null);

  useEffect(() => {
    if (!working?.active) {
      setShown(null);
      return;
    }
    setShown(working);
    const lapse = setTimeout(() => {
      setShown((current) => (current === working ? null : current));
    }, working.ttlSeconds * 1000);
    return () => {
      clearTimeout(lapse);
    };
  }, [working]);

  useEffect(() => {
    if (ended || !streamHealthy) setShown(null);
  }, [ended, streamHealthy]);

  const clearTyping = useCallback(() => {
    setShown(null);
  }, []);

  return { typing: shown !== null && !ended && streamHealthy, clearTyping };
}
