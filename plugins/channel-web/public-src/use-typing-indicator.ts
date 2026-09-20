/**
 * The "agent is typing" bubble: it runs from the moment a send is ACCEPTED until
 * something of the agent's lands after that point, and clears on anything that ends
 * the wait — a dead session, or a stream that can no longer carry the reply.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import type { ChatItem } from '@/transcript-model';

export interface TypingIndicator {
  /** Whether the typing bubble is showing. */
  readonly typing: boolean;
  /** Start the wait from the current transcript tail — called when a send is
   * accepted, reading the LIVE item count rather than a stale captured one. */
  readonly markAwaitingReply: () => void;
  /** Stop the wait — a refused send, or a new conversation. */
  readonly clearTyping: () => void;
}

export function useTypingIndicator(params: {
  items: readonly ChatItem[];
  ended: boolean;
  streamHealthy: boolean;
}): TypingIndicator {
  const { items, ended, streamHealthy } = params;
  // The index into `items` at the moment a send was accepted. The agent is
  // "typing" until something of theirs lands after that point.
  const [typingFrom, setTypingFrom] = useState<number | null>(null);

  // The live item count, read when a send is ACCEPTED: the count captured when
  // the visitor pressed send is stale by then, and a stale mark lets an item that
  // arrived before this send count as the reply to it.
  const itemCountRef = useRef(0);
  useEffect(() => {
    itemCountRef.current = items.length;
  }, [items.length]);

  // The typing bubble clears on the agent's next turn — a message of theirs, a
  // question, or a media card — and on anything that ends the wait: a dead
  // session, or a stream that can no longer carry the reply.
  useEffect(() => {
    if (typingFrom === null) return;
    if (ended || !streamHealthy) {
      setTypingFrom(null);
      return;
    }
    const replied = items
      .slice(typingFrom)
      .some((item) => item.kind !== 'message' || item.direction === 'out');
    if (replied) setTypingFrom(null);
  }, [items, typingFrom, ended, streamHealthy]);

  const markAwaitingReply = useCallback(() => {
    setTypingFrom(itemCountRef.current);
  }, []);
  const clearTyping = useCallback(() => {
    setTypingFrom(null);
  }, []);

  return { typing: typingFrom !== null, markAwaitingReply, clearTyping };
}
