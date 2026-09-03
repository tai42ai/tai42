/**
 * The public chat page.
 *
 * Layout is header / transcript / composer inside a viewport-height column, so
 * only the transcript scrolls and the composer never leaves the screen. On a
 * touch device the column is sized from `visualViewport` instead of the layout
 * viewport, which is what keeps the composer above the on-screen keyboard.
 *
 * A message the visitor sends is rendered OPTIMISTICALLY: the bubble appears at
 * once carrying a pending mark, turns to a sent mark when the door accepts it,
 * and is retired when the transcript frame for that same message arrives — matched
 * by the `message_id` the door returned, or by the idempotency key the frame echoes
 * back, which is the only match left when that answer was lost.
 * A refused send keeps its bubble, wearing the reason and a Retry — a message is
 * never silently lost, and the visitor never reads a raw error.
 *
 * The session cookie is the whole credential, so a session the server no longer
 * knows is TERMINAL for this page: sending, answering and reconnecting all stop
 * and the visitor is asked to reload, which is the one thing that mints a new one.
 */
import type { ReactElement, RefObject } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ConfirmDialog, ErrorState, Spinner } from '@tai42/studio-sdk';

import { answerQuestion, isSessionMissing, rotateSession, sendMessage, submitForm } from '@/api';
import { Composer } from '@/composer';
import { Header } from '@/header';
import { Transcript, type TranscriptEntry } from '@/transcript';
import { useChatStream } from '@/use-chat-stream';
import type { SendStatus } from '@/bubble';

/** One message this page sent, held until its transcript frame comes back. */
interface OutboxItem {
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

/**
 * Bind the page's height to the VISUAL viewport. On a phone the on-screen
 * keyboard shrinks the visual viewport but not the layout viewport, so a
 * `100dvh` column would keep the composer underneath the keyboard; `offsetTop`
 * follows the same shift when the browser scrolls the visual viewport to reveal
 * the focused field. Where the API is absent the stylesheet's `100dvh` stands.
 */
function useViewportFit(ref: RefObject<HTMLDivElement | null>): void {
  useEffect(() => {
    const viewport = window.visualViewport;
    const el = ref.current;
    if (!viewport || el === null) return;
    const apply = (): void => {
      el.style.setProperty('--tcw-vh', `${viewport.height}px`);
      el.style.setProperty('--tcw-vv-top', `${viewport.offsetTop}px`);
    };
    apply();
    viewport.addEventListener('resize', apply);
    viewport.addEventListener('scroll', apply);
    return () => {
      viewport.removeEventListener('resize', apply);
      viewport.removeEventListener('scroll', apply);
    };
  }, [ref]);
}

export interface ChatAppProps {
  /** The web route this page talks to, read from the shell's `data-identity`. */
  readonly identity: string;
  /** The conversation's name — the operator's page title. */
  readonly title: string;
}

export function ChatApp({ identity, title }: ChatAppProps): ReactElement {
  const [epoch, setEpoch] = useState(0);
  const stream = useChatStream(identity, epoch);
  const [outbox, setOutbox] = useState<readonly OutboxItem[]>([]);
  const [draft, setDraft] = useState('');
  const [sessionEnded, setSessionEnded] = useState(false);
  // The index into `stream.items` at the moment a send was accepted. The agent is
  // "typing" until something of theirs lands after that point.
  const [typingFrom, setTypingFrom] = useState<number | null>(null);
  // Bumped on every send: the transcript takes it as "return to the tail".
  const [pinToken, setPinToken] = useState(0);
  const [confirmingReset, setConfirmingReset] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState<Error | null>(null);

  // Which session the page is on. Bumped the moment a rotate succeeds — before any
  // render — so a send that was already in flight can tell that its answer belongs
  // to the conversation the visitor has just left.
  const generationRef = useRef(0);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  useViewportFit(rootRef);

  // The entry code carried in the page URL (`?tai_entry=…`), read ONCE. A rotation
  // on a gated route re-presents it so the fresh session is admitted. It is never
  // stripped from the URL — a reload must re-present it to the page door.
  const entryCode = useMemo(() => new URLSearchParams(window.location.search).get('tai_entry'), []);

  const items = stream.items;
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
  const ended = sessionEnded || stream.sessionExpired;

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
  const streamHealthy = stream.connected && stream.error === null;
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

  const deliver = useCallback(
    (localId: string, text: string, clientMessageId: string) => {
      // A message the visitor just sent always returns them to the tail.
      setPinToken((current) => current + 1);
      // The send this outcome belongs to. A "new conversation" started while it was
      // in flight retires the whole outbox with the session that owned it, so
      // neither outcome may touch the fresh one: a stale success would leave it
      // waiting on a reply nobody owes, and a stale `session_missing` would kill a
      // session that is alive.
      const generation = generationRef.current;
      sendMessage(identity, text, clientMessageId).then(
        (messageId) => {
          if (generationRef.current !== generation) return;
          setOutbox((prev) =>
            prev.map((item) =>
              item.localId === localId ? { ...item, status: 'sent', error: null, messageId } : item,
            ),
          );
          setTypingFrom(itemCountRef.current);
        },
        (err: unknown) => {
          if (generationRef.current !== generation) return;
          const message = err instanceof Error ? err.message : String(err);
          setOutbox((prev) =>
            prev.map((item) =>
              item.localId === localId ? { ...item, status: 'failed', error: message } : item,
            ),
          );
          setTypingFrom(null);
          if (isSessionMissing(err)) setSessionEnded(true);
        },
      );
    },
    [identity],
  );

  // The one send door. Validates the text, records the optimistic bubble and hands
  // it to `deliver`; it never touches the composer draft, so a chip tap can send its
  // own text while the visitor's typed-but-unsent draft stays put. Reports whether a
  // message actually went out, which is what lets the composer clear its own draft.
  const send = useCallback(
    (raw: string): boolean => {
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
        },
      ]);
      composerRef.current?.focus();
      deliver(localId, text, clientMessageId);
      return true;
    },
    [ended, deliver],
  );

  // The composer's own submission: send the draft and, only if it went out, clear it.
  const onSend = useCallback(() => {
    if (send(draft)) setDraft('');
  }, [draft, send]);

  // A pair code carried in the page URL (`?tai_pair=…`) is submitted ONCE as the visitor's
  // first message and then stripped from the URL, so a reload or a shared link cannot
  // resubmit it. The session cookie is already minted by the navigation that served this
  // page, so the send needs no further wait. A `pair` that does not fully match the code
  // shape is ignored entirely — never submitted, never stripped, never reflected.
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
      // recognise a retry of a delivery it already accepted.
      deliver(localId, item.text, item.clientMessageId);
    },
    [outbox, ended, deliver],
  );

  const onAnswer = useCallback(async (interactionId: string, answer: unknown): Promise<void> => {
    // The conversation this answer belongs to, read before it leaves. A "new
    // conversation" started while it was in flight drops the session it was sent
    // on, so its `session_missing` describes the conversation the visitor has
    // just left — applying it would end the fresh one the rotate just minted.
    const generation = generationRef.current;
    try {
      await answerQuestion(interactionId, answer);
    } catch (err) {
      if (generationRef.current === generation && isSessionMissing(err)) setSessionEnded(true);
      throw err;
    }
  }, []);

  const onSubmitForm = useCallback(
    async (token: string, values: Record<string, unknown>): Promise<void> => {
      // Same stale-session guard as an answer: a rotate that landed while this
      // submission was in flight makes its `session_missing` describe the
      // conversation the visitor just left, never the fresh one.
      const generation = generationRef.current;
      try {
        await submitForm(token, values);
      } catch (err) {
        if (generationRef.current === generation && isSessionMissing(err)) setSessionEnded(true);
        throw err;
      }
    },
    [],
  );

  const focusComposer = useCallback(() => {
    composerRef.current?.focus();
  }, []);

  const confirmReset = useCallback(() => {
    setResetting(true);
    setResetError(null);
    rotateSession(identity, entryCode).then(
      () => {
        generationRef.current += 1;
        setResetting(false);
        setConfirmingReset(false);
        setOutbox([]);
        setDraft('');
        setTypingFrom(null);
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
  }, [identity, entryCode]);

  const entries = useMemo<readonly TranscriptEntry[]>(() => {
    const fromStream: TranscriptEntry[] = items.map((item) => {
      if (item.kind === 'message') {
        return {
          kind: 'message',
          key: item.id,
          direction: item.direction,
          text: item.text,
          ts: item.ts,
          status: null,
          error: null,
          retryId: null,
        };
      }
      if (item.kind === 'media') {
        return { kind: 'media', key: item.id, ts: item.ts, item };
      }
      if (item.kind === 'form') {
        return { kind: 'form', key: item.id, ts: item.ts, item };
      }
      return { kind: 'question', key: item.id, ts: item.ts, question: item };
    });
    const unconfirmed: TranscriptEntry[] = pending.map((item) => ({
      kind: 'message',
      key: item.localId,
      direction: 'in',
      text: item.text,
      ts: item.ts,
      status: item.status,
      error: item.error,
      retryId: item.status === 'failed' ? item.localId : null,
    }));
    return [...fromStream, ...unconfirmed];
  }, [items, pending]);

  const reconnecting = stream.backlogLoaded && !stream.connected && !stream.disabled && !ended;
  const bodyIsBroken =
    stream.disabled || (!stream.backlogLoaded && stream.error !== null && !ended);
  // A frame the page could not read on a conversation that is otherwise up. The
  // stream carries on, so this says something was lost without taking the page
  // away — the hook never drops a bad frame in silence.
  const frameDropped = stream.backlogLoaded && stream.connected && stream.error !== null && !ended;

  return (
    <div className="tcw-app" ref={rootRef}>
      <Header
        title={title}
        connected={stream.connected}
        disabled={ended || resetting}
        onNewConversation={() => setConfirmingReset(true)}
      />
      {ended ? (
        <p className="tcw-banner" role="alert">
          <span>This conversation has ended. Reload the page to start a new one.</span>
          <button
            type="button"
            className="tcw-banner-action"
            onClick={() => window.location.reload()}
          >
            Reload
          </button>
        </p>
      ) : null}
      {bodyIsBroken ? (
        <div className="tcw-centered tcw-centered--grow">
          <ErrorState
            message={
              stream.disabled
                ? 'Chat is not switched on for this deployment yet.'
                : "We can't reach the conversation right now — it will reconnect on its own."
            }
            {...(stream.disabled ? {} : { onRetry: () => setEpoch((current) => current + 1) })}
          />
        </div>
      ) : (
        <Transcript
          // Keyed by the epoch, so a new conversation gets a NEW transcript:
          // where the visitor had scrolled to, and what they had already seen,
          // describe the conversation they left and must not outlive it.
          key={epoch}
          entries={entries}
          answeredIds={stream.answeredIds}
          typing={typingFrom !== null}
          loading={!stream.backlogLoaded && stream.error === null && !ended}
          locked={ended}
          onAnswer={onAnswer}
          onAnswered={focusComposer}
          onRetry={onRetry}
          onSend={send}
          onSubmitForm={onSubmitForm}
          pinToken={pinToken}
        />
      )}
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
      <Composer
        value={draft}
        onChange={setDraft}
        onSend={onSend}
        disabled={ended}
        placeholder={ended ? 'Reload the page to keep chatting' : 'Write a message…'}
        inputRef={composerRef}
      />
      {confirmingReset ? (
        <ConfirmDialog
          title="Start a new conversation?"
          confirmLabel="Start new"
          pendingLabel="Starting"
          confirmVariant="primary"
          isPending={resetting}
          error={resetError}
          onConfirm={confirmReset}
          onClose={() => {
            setConfirmingReset(false);
            setResetError(null);
          }}
        >
          This clears the conversation on screen and starts a fresh one. You will not be able to
          come back to this one.
        </ConfirmDialog>
      ) : null}
    </div>
  );
}
