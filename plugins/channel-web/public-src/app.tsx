/**
 * The public chat page.
 *
 * Layout is header / transcript / composer inside a viewport-height column, so
 * only the transcript scrolls and the composer never leaves the screen. On a
 * touch device the column is sized from `visualViewport` instead of the layout
 * viewport, which is what keeps the composer above the on-screen keyboard.
 *
 * `ChatApp` is the composition root: it holds the page's two DOM refs and the
 * draft, wires the transcript stream to the outbox, typing indicator, answer/submit
 * doors and conversation-reset lifecycle, and renders the header, body, status
 * pills, composer and reset dialog from their derived flags.
 *
 * The session cookie is the whole credential, so a session the server no longer
 * knows is TERMINAL for this page: sending, answering and reconnecting all stop
 * and the visitor is asked to reload, which is the one thing that mints a new one.
 */
import type { ReactElement } from 'react';
import { useCallback, useMemo, useRef, useState } from 'react';

import { EndedBanner } from '@/chat-banner';
import { BrokenBody } from '@/chat-broken-body';
import { ConnectionStatus } from '@/chat-status';
import { Composer } from '@/composer';
import { Header } from '@/header';
import { ResetConfirmDialog } from '@/reset-confirm-dialog';
import { Transcript } from '@/transcript';
import { buildTranscriptEntries } from '@/transcript-entries';
import { useDropZone, useUploads } from '@/uploads';
import { useAnswerSubmit } from '@/use-answer-submit';
import { useChatStream } from '@/use-chat-stream';
import { useConversationReset } from '@/use-conversation-reset';
import { useOutbox } from '@/use-outbox';
import { useTypingIndicator } from '@/use-typing-indicator';
import { useViewportFit } from '@/use-viewport-fit';

export interface ChatAppProps {
  /** The web route this page talks to, read from the shell's `data-identity`. */
  readonly identity: string;
  /** The conversation's name — the operator's page title. */
  readonly title: string;
}

interface ChatBodyFlags {
  /** Backlog is in but the live stream is down and the session is still open. */
  readonly reconnecting: boolean;
  /** No usable body: the route is disabled, or the backlog failed to load. */
  readonly bodyIsBroken: boolean;
  /** A live stream that is up but dropped a frame mid-session. */
  readonly frameDropped: boolean;
  /** Backlog still loading, no error, session open — show the spinner. */
  readonly loading: boolean;
}

/** The transcript-body view flags derived from the stream state and whether the
 * session has ended, kept out of the component so its own branching stays small. */
function deriveChatBodyFlags(args: {
  backlogLoaded: boolean;
  connected: boolean;
  disabled: boolean;
  hasError: boolean;
  ended: boolean;
}): ChatBodyFlags {
  const { backlogLoaded, connected, disabled, hasError, ended } = args;
  return {
    reconnecting: backlogLoaded && !connected && !disabled && !ended,
    bodyIsBroken: disabled || (!backlogLoaded && hasError && !ended),
    frameDropped: backlogLoaded && connected && hasError && !ended,
    loading: !backlogLoaded && !hasError && !ended,
  };
}

/** The per-message attachment cap the page shell advertises on `#root`, read once at
 * mount. Absent → `null`: the tray applies no client-side cap and the messages door's
 * 422 remains the sole (authoritative) enforcer. Present but not a positive integer is
 * a malformed shell and throws loudly rather than degrading to a silent guess. */
function readAttachmentCap(): number | null {
  const raw = document.getElementById('root')?.dataset.maxAttachments;
  if (raw === undefined) return null;
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`the chat page shell carries an invalid data-max-attachments: ${raw}`);
  }
  return value;
}

export function ChatApp({ identity, title }: ChatAppProps): ReactElement {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  useViewportFit(rootRef);

  const [draft, setDraft] = useState('');

  // The entry code carried in the page URL (`?tai_entry=…`), read ONCE. A rotation
  // on a gated route re-presents it so the fresh session is admitted. It is never
  // stripped from the URL — a reload must re-present it to the page door.
  const entryCode = useMemo(() => new URLSearchParams(window.location.search).get('tai_entry'), []);

  // Clears the page's own local state on a successful rotate. Held in a ref because
  // the reset hook is created before the outbox/typing hooks it clears; the ref is
  // pointed at the current setters below, and the rotate runs it synchronously so
  // the old conversation's bubbles cannot flash into the fresh one.
  const resetLocalStateRef = useRef<() => void>(() => {
    // Pointed at the current setters below; a no-op until then.
  });
  const reset = useConversationReset({ identity, entryCode, resetLocalStateRef });

  const stream = useChatStream(identity, reset.epoch);
  const items = stream.items;
  const ended = reset.sessionEnded || stream.sessionExpired;
  const streamHealthy = stream.connected && stream.error === null;

  const typing = useTypingIndicator({ items, ended, streamHealthy });
  const outbox = useOutbox({
    identity,
    items,
    ended,
    generationRef: reset.generationRef,
    composerRef,
    onSessionEnded: reset.endConversation,
    markAwaitingReply: typing.markAwaitingReply,
    clearTyping: typing.clearTyping,
  });
  const answer = useAnswerSubmit({
    generationRef: reset.generationRef,
    onSessionEnded: reset.endConversation,
  });
  const maxAttachments = useMemo(() => readAttachmentCap(), []);
  const uploads = useUploads({
    identity,
    connected: stream.connected,
    maxAttachments,
    onSessionEnded: reset.endConversation,
  });

  resetLocalStateRef.current = () => {
    outbox.clear();
    uploads.clear();
    setDraft('');
    typing.clearTyping();
  };

  const send = outbox.send;
  // The composer's own submission: send the draft with its ready attachments and,
  // only if it went out, clear the draft and drop JUST the attachments that were sent
  // — a failed or offline-queued item that was not part of this send stays in the tray
  // with its retry/queue affordance.
  const onSend = useCallback(() => {
    const sentIds = uploads.readyMediaIds;
    if (send(draft, null, sentIds, uploads.readyMedia)) {
      setDraft('');
      uploads.removeIds(sentIds);
    }
  }, [draft, send, uploads]);
  const focusComposer = useCallback(() => {
    composerRef.current?.focus();
  }, []);
  // Removing or cancelling a tray item destroys the control that had focus, so it
  // returns to the composer where the next action is.
  const onRemoveAttachment = useCallback(
    (id: string) => {
      uploads.remove(id);
      focusComposer();
    },
    [uploads, focusComposer],
  );
  const dropZone = useDropZone(uploads.addFiles, !ended);

  const entries = useMemo(
    () => buildTranscriptEntries(items, outbox.pending),
    [items, outbox.pending],
  );

  const { reconnecting, bodyIsBroken, frameDropped, loading } = deriveChatBodyFlags({
    backlogLoaded: stream.backlogLoaded,
    connected: stream.connected,
    disabled: stream.disabled,
    hasError: stream.error !== null,
    ended,
  });

  return (
    <div className="tcw-app" ref={rootRef}>
      <Header
        title={title}
        connected={stream.connected}
        disabled={ended || reset.resetting}
        onNewConversation={reset.openReset}
      />
      {ended ? <EndedBanner /> : null}
      {bodyIsBroken ? (
        <BrokenBody disabled={stream.disabled} onRetry={reset.restartStream} />
      ) : (
        <div
          className={dropZone.active ? 'tcw-drop-zone tcw-drop-active' : 'tcw-drop-zone'}
          onDragEnter={dropZone.onDragEnter}
          onDragOver={dropZone.onDragOver}
          onDragLeave={dropZone.onDragLeave}
          onDrop={dropZone.onDrop}
          data-testid="drop-zone"
        >
          <Transcript
            // Keyed by the epoch, so a new conversation gets a NEW transcript:
            // where the visitor had scrolled to, and what they had already seen,
            // describe the conversation they left and must not outlive it.
            key={reset.epoch}
            entries={entries}
            answeredIds={stream.answeredIds}
            typing={typing.typing}
            loading={loading}
            locked={ended}
            onAnswer={answer.onAnswer}
            onAnswered={focusComposer}
            onRetry={outbox.onRetry}
            onSend={send}
            onSubmitForm={answer.onSubmitForm}
            pinToken={outbox.pinToken}
          />
        </div>
      )}
      <ConnectionStatus reconnecting={reconnecting} frameDropped={frameDropped} />
      <Composer
        value={draft}
        onChange={setDraft}
        onSend={onSend}
        disabled={ended}
        placeholder={ended ? 'Reload the page to keep chatting' : 'Write a message…'}
        inputRef={composerRef}
        attachments={uploads.items}
        onAddFiles={uploads.addFiles}
        onRemoveAttachment={onRemoveAttachment}
        onRetryAttachment={uploads.retry}
        attachAnnouncement={uploads.announcement}
        uploadsInFlight={uploads.anyInFlight}
        attachAtCap={uploads.atCap}
        maxAttachments={maxAttachments}
        onAttachBlocked={uploads.announceCap}
      />
      {reset.confirmingReset ? (
        <ResetConfirmDialog
          isPending={reset.resetting}
          error={reset.resetError}
          onConfirm={reset.confirmReset}
          onClose={reset.closeReset}
        />
      ) : null}
    </div>
  );
}
