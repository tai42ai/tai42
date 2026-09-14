/**
 * The optimistic outbox: a message the visitor sends is rendered at once carrying a
 * pending mark, turns to a sent mark when the door accepts it, and is retired when
 * the transcript frame for that same message arrives — matched by the `message_id`
 * the door returned, or by the idempotency key the frame echoes back, which is the
 * only match left when that answer was lost. A refused send keeps its bubble,
 * wearing the reason and a Retry — a message is never silently lost.
 *
 * A pair code carried in the page URL (`?tai_pair=…`) is submitted ONCE as the
 * visitor's first message and then stripped from the URL.
 */
import type { RefObject } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { isSessionMissing, sendMessage } from '@/api';
import type { SendStatus } from '@/bubble';
import type { ChatItem } from '@/transcript-model';

/** One message this page sent, held until its transcript frame comes back. */
export interface OutboxItem {
  readonly localId: string;
  readonly text: string;
  readonly ts: string;
  readonly status: SendStatus;
  /** The visitor-facing reason, on a send that did not land. */
  readonly error: string | null;
  /** The bridge id the door returned — the frame carrying it retires this item. */
  readonly messageId: string | null;
  /** This message's idempotency key, minted ONCE and re-sent by every retry. */
  readonly clientMessageId: string;
  /** The tapped reply option's authored id, when this send is a reply chip carrying
   * one. It rides the send (and every retry of it) as `params.reply_id`; `null` on a
   * typed message or a chip without an id. */
  readonly replyId: string | null;
}

/**
 * A pair code carried in the page URL as `?tai_pair=…`. The visitor followed an invite
 * link, so the page submits the code ONCE as their first message and the server-side
 * intercept redeems it exactly as if they had typed it. Only a value that FULLY matches
 * this shape is acted on — anything else is ignored entirely, never submitted and never
 * reflected back into the page.
 */
const PAIR_CODE_RE = /^LINK-[A-Z0-9]{8}$/;

let localIdSeq = 0;
function nextLocalId(): string {
  localIdSeq += 1;
  return `local-${localIdSeq.toString(36)}`;
}

/**
 * A fresh idempotency key for one composed message. The door derives the delivery's
 * provider message id from it, so an attempt that succeeded server-side but whose
 * response never arrived is DEDUPED on retry instead of delivered twice — which
 * only holds while every attempt at the same message carries the same key.
 */
function nextClientMessageId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/**
 * Still waiting for its own transcript frame. Once that frame is on the stream the
 * real entry carries the message and the optimistic bubble must go.
 *
 * The frame is matched two ways, because either half of the pair can be missing.
 * The door's answer gives this page the entry's `message_id` — the usual match. A
 * send whose ANSWER was lost never learns that id, so its bubble would sit beside
 * the real message as a permanent visible duplicate; the door echoes the message's
 * own idempotency key back onto the frame, and that identifies the entry as this
 * send's without the id ever arriving.
 */
function isPending(
  item: OutboxItem,
  itemIds: ReadonlySet<string>,
  echoedKeys: ReadonlySet<string>,
): boolean {
  if (echoedKeys.has(item.clientMessageId)) return false;
  return item.messageId === null || !itemIds.has(item.messageId);
}

export interface Outbox {
  /** The bubbles still owed a transcript frame. */
  readonly pending: readonly OutboxItem[];
  /** Bumped on every send: the transcript takes it as "return to the tail". */
  readonly pinToken: number;
  /** The one send door. Reports whether a message actually went out, which is what
   * lets the composer clear its own draft. */
  readonly send: (raw: string, replyId?: string | null) => boolean;
  readonly onRetry: (localId: string) => void;
  /** Drop every optimistic bubble (a new conversation). */
  readonly clear: () => void;
}

export function useOutbox(params: {
  identity: string;
  items: readonly ChatItem[];
  ended: boolean;
  generationRef: RefObject<number>;
  composerRef: RefObject<HTMLTextAreaElement | null>;
  onSessionEnded: () => void;
  markAwaitingReply: () => void;
  clearTyping: () => void;
}): Outbox {
  const { identity, items, ended, generationRef, composerRef } = params;
  const { onSessionEnded, markAwaitingReply, clearTyping } = params;
  const [outbox, setOutbox] = useState<readonly OutboxItem[]>([]);
  const [pinToken, setPinToken] = useState(0);

  const itemIds = useMemo(() => new Set(items.map((item) => item.id)), [items]);
  // The idempotency keys the transcript has echoed back — the second way a frame
  // is matched to the bubble that is standing in for it.
  const echoedKeys = useMemo(
    () =>
      new Set(
        items.flatMap((item) =>
          item.kind === 'message' && item.clientMessageId !== null ? [item.clientMessageId] : [],
        ),
      ),
    [items],
  );

  // The bubbles still owed a transcript frame. DERIVED, never retired by an
  // effect: the frame and the POST response race — the door writes the entry
  // before it answers — so which one lands first must not decide anything.
  const pending = useMemo(
    () => outbox.filter((item) => isPending(item, itemIds, echoedKeys)),
    [outbox, itemIds, echoedKeys],
  );

  // Housekeeping: drop retired items from state so the outbox stays bounded over
  // a long session. The render reads `pending`, so this decides nothing on screen.
  useEffect(() => {
    setOutbox((prev) => {
      const next = prev.filter((item) => isPending(item, itemIds, echoedKeys));
      return next.length === prev.length ? prev : next;
    });
  }, [itemIds, echoedKeys, outbox]);

  const deliver = useCallback(
    (localId: string, text: string, clientMessageId: string, replyId: string | null) => {
      // A message the visitor just sent always returns them to the tail.
      setPinToken((current) => current + 1);
      // The send this outcome belongs to. A "new conversation" started while it was
      // in flight retires the whole outbox with the session that owned it, so
      // neither outcome may touch the fresh one.
      const generation = generationRef.current;
      sendMessage(identity, text, clientMessageId, replyId).then(
        (messageId) => {
          if (generationRef.current !== generation) return;
          setOutbox((prev) =>
            prev.map((item) =>
              item.localId === localId ? { ...item, status: 'sent', error: null, messageId } : item,
            ),
          );
          markAwaitingReply();
        },
        (err: unknown) => {
          if (generationRef.current !== generation) return;
          const message = err instanceof Error ? err.message : String(err);
          setOutbox((prev) =>
            prev.map((item) =>
              item.localId === localId ? { ...item, status: 'failed', error: message } : item,
            ),
          );
          clearTyping();
          if (isSessionMissing(err)) onSessionEnded();
        },
      );
    },
    [identity, generationRef, markAwaitingReply, clearTyping, onSessionEnded],
  );

  // The one send door. Validates the text, records the optimistic bubble and hands
  // it to `deliver`; it never touches the composer draft, so a chip tap can send its
  // own text while the visitor's typed-but-unsent draft stays put.
  const send = useCallback(
    (raw: string, replyId: string | null = null): boolean => {
      const text = raw.trim();
      if (text === '' || ended) return false;
      const localId = nextLocalId();
      const clientMessageId = nextClientMessageId();
      setOutbox((prev) => [
        ...prev,
        {
          localId,
          text,
          ts: new Date().toISOString(),
          status: 'sending',
          error: null,
          messageId: null,
          clientMessageId,
          replyId,
        },
      ]);
      composerRef.current?.focus();
      deliver(localId, text, clientMessageId, replyId);
      return true;
    },
    [ended, deliver, composerRef],
  );

  const onRetry = useCallback(
    (localId: string) => {
      const item = outbox.find((candidate) => candidate.localId === localId);
      if (item === undefined || ended) return;
      setOutbox((prev) =>
        prev.map((candidate) =>
          candidate.localId === localId
            ? { ...candidate, status: 'sending', error: null }
            : candidate,
        ),
      );
      // The SAME idempotency key as the first attempt: that is what lets the door
      // recognise a retry of a delivery it already accepted. The tapped reply id (if
      // any) rides the retry too, so a re-sent chip carries the same enrichment.
      deliver(localId, item.text, item.clientMessageId, item.replyId);
    },
    [outbox, ended, deliver],
  );

  // A pair code carried in the page URL is submitted ONCE as the visitor's first
  // message and then stripped, so a reload or a shared link cannot resubmit it. A
  // `pair` that does not fully match the code shape is ignored entirely.
  const pairConsumedRef = useRef(false);
  useEffect(() => {
    if (pairConsumedRef.current) return;
    pairConsumedRef.current = true;
    const pair = new URLSearchParams(window.location.search).get('tai_pair');
    if (pair === null || !PAIR_CODE_RE.test(pair)) return;
    send(pair);
    const url = new URL(window.location.href);
    url.searchParams.delete('tai_pair');
    window.history.replaceState(
      window.history.state,
      '',
      `${url.pathname}${url.search}${url.hash}`,
    );
  }, [send]);

  const clear = useCallback(() => {
    setOutbox([]);
  }, []);

  return { pending, pinToken, send, onRetry, clear };
}
