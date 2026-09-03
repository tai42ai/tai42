/**
 * The scrolling transcript: day dividers, gap-based message grouping, the
 * typing bubble, and tail-following with a "Jump to latest" escape hatch.
 *
 * SCROLL. While the reader is at the tail, new content pulls the view down;
 * scrolling up detaches the follow and reveals the jump button, which carries the
 * count of everything that arrived while detached. A send re-attaches it — the
 * visitor's own message is never something they missed. The jump is INSTANT and
 * CONTAINER-ONLY: a smooth or window-level scroll emits intermediate non-bottom
 * scroll events that flip the follow off again and drag the composer out of view.
 *
 * GROUPING is pure ({@link buildRows}) — one row per entry plus a divider per new
 * day, with a time stamp on the first entry of each group. A group breaks on a new
 * day, on a gap of {@link GROUP_GAP_MS}, or when the speaker changes.
 */
import type { ReactElement } from 'react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ArrowDownIcon, Button, EmptyState, Spinner } from '@tai42/studio-sdk';

import { Bubble, type SendStatus } from '@/bubble';
import { FormCard, type FormCardItem } from '@/form-card';
import { MediaCard, type MediaCardItem } from '@/media-card';
import { QuestionCard, type QuestionItem } from '@/question-card';

/** How close to the bottom edge (px) still counts as "pinned to the tail". */
const BOTTOM_SLACK_PX = 32;

/** A silence this long starts a new message group (and a new time stamp). */
export const GROUP_GAP_MS = 5 * 60_000;

/** Whether the scroll region is at (or within slack of) its bottom edge. */
export function isAtBottom(
  el: Pick<HTMLElement, 'scrollHeight' | 'scrollTop' | 'clientHeight'>,
  slack: number = BOTTOM_SLACK_PX,
): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= slack;
}

/** One thing to render, whether it came off the stream or is still in flight. */
export type TranscriptEntry =
  | {
      readonly kind: 'message';
      readonly key: string;
      readonly direction: 'in' | 'out';
      readonly text: string;
      readonly ts: string;
      /** Non-null only while the message is the visitor's own and unconfirmed. */
      readonly status: SendStatus | null;
      readonly error: string | null;
      /** The outbox key a retry re-sends, on a failed send only. */
      readonly retryId: string | null;
    }
  | {
      readonly kind: 'question';
      readonly key: string;
      readonly ts: string;
      readonly question: QuestionItem;
    }
  | {
      readonly kind: 'media';
      readonly key: string;
      readonly ts: string;
      readonly item: MediaCardItem;
    }
  | {
      readonly kind: 'form';
      readonly key: string;
      readonly ts: string;
      readonly item: FormCardItem;
    };

/** A rendered row: a day divider, or one entry with its grouping decisions. */
export type TranscriptRow =
  | { readonly kind: 'day'; readonly key: string; readonly label: string }
  | {
      readonly kind: 'entry';
      readonly key: string;
      readonly entry: TranscriptEntry;
      readonly groupStart: boolean;
      /** The group's time stamp — set on the first entry of a group only. */
      readonly time: string | null;
    };

function startOfDay(at: number): number {
  const date = new Date(at);
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}

/** The next local midnight after `at` — the moment every day divider's label
 * changes. Taken off the calendar rather than by adding 24 hours, so the day a
 * clock change makes 23 or 25 hours long still lands on its own boundary. */
export function startOfNextDay(at: number): number {
  const date = new Date(at);
  date.setHours(24, 0, 0, 0);
  return date.getTime();
}

/** "Today" / "Yesterday" / the written date, in the viewer's own locale. */
export function dayLabel(at: number, now: number): string {
  const days = Math.round((startOfDay(now) - startOfDay(at)) / 86_400_000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  const sameYear = new Date(at).getFullYear() === new Date(now).getFullYear();
  return new Date(at).toLocaleDateString(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    ...(sameYear ? {} : { year: 'numeric' }),
  });
}

function timeLabel(at: number): string {
  return new Date(at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/** Who is speaking — a question or a media card is the agent's turn, like an
 * outbound message. */
function speakerOf(entry: TranscriptEntry): 'in' | 'out' {
  return entry.kind === 'message' ? entry.direction : 'out';
}

/**
 * Fold entries into rows. Pure and total: entries are assumed to be in arrival
 * order and to carry parsable timestamps (the stream reducer refuses any frame
 * whose timestamp does not parse), so no row is ever dropped here.
 */
export function buildRows(entries: readonly TranscriptEntry[], now: number): TranscriptRow[] {
  const rows: TranscriptRow[] = [];
  let previousAt: number | null = null;
  let previousSpeaker: 'in' | 'out' | null = null;
  for (const entry of entries) {
    const at = Date.parse(entry.ts);
    const newDay = previousAt === null || startOfDay(at) !== startOfDay(previousAt);
    if (newDay) rows.push({ kind: 'day', key: `day-${entry.key}`, label: dayLabel(at, now) });
    const speaker = speakerOf(entry);
    const groupStart =
      newDay ||
      speaker !== previousSpeaker ||
      previousAt === null ||
      at - previousAt >= GROUP_GAP_MS;
    rows.push({
      kind: 'entry',
      key: entry.key,
      entry,
      groupStart,
      time: groupStart ? timeLabel(at) : null,
    });
    previousAt = at;
    previousSpeaker = speaker;
  }
  return rows;
}

export interface TranscriptProps {
  readonly entries: readonly TranscriptEntry[];
  /** Interaction ids settled by a `chat.answered` frame. */
  readonly answeredIds: ReadonlySet<string>;
  /** The agent is composing a reply — shown as the animated three-dot bubble. */
  readonly typing: boolean;
  /** The backlog has not arrived yet: show the loader, not an empty conversation. */
  readonly loading: boolean;
  /** No answer can land (an ended session), so every question widget is inert. */
  readonly locked: boolean;
  readonly onAnswer: (interactionId: string, answer: unknown) => Promise<void>;
  readonly onAnswered: () => void;
  readonly onRetry: (retryId: string) => void;
  /** Sends a media card chip's label as a regular visitor message — the same send
   * door the composer uses. */
  readonly onSend: (text: string) => void;
  /** Submits one ask-less form card's values through its token door. */
  readonly onSubmitForm: (token: string, values: Record<string, unknown>) => Promise<void>;
  /** Bumped on every send from this page. The visitor's own message always
   * returns them to the tail and never counts as something they missed. */
  readonly pinToken: number;
}

export function Transcript({
  entries,
  answeredIds,
  typing,
  loading,
  locked,
  onAnswer,
  onAnswered,
  onRetry,
  onSend,
  onSubmitForm,
  pinToken,
}: TranscriptProps): ReactElement {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [pinned, setPinned] = useState(true);
  const [seen, setSeen] = useState(0);
  const [announce, setAnnounce] = useState(false);
  // The day the dividers are labelled against. "Today" and "Yesterday" are read
  // off it, so a page left open across midnight would keep yesterday's labels
  // until something else arrived; one timer per day re-dates them on the boundary
  // itself. The state holds the BOUNDARY rather than the wall clock, so a timer
  // that fires a moment early cannot re-arm itself for the same midnight twice.
  const [today, setToday] = useState<number>(() => Date.now());
  useEffect(() => {
    const midnight = startOfNextDay(today);
    const timer = setTimeout(() => setToday(midnight), Math.max(0, midnight - Date.now()));
    return () => clearTimeout(timer);
  }, [today]);
  const rows = useMemo(() => buildRows(entries, today), [entries, today]);

  // A manual scroll re-decides whether we are still at the tail: scrolling up
  // detaches the follow (and reveals the jump affordance); scrolling back down
  // re-arms it.
  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (el === null) return;
    setPinned(isAtBottom(el));
  }, []);

  const scrollToTail = useCallback(() => {
    const el = scrollRef.current;
    if (el !== null) el.scrollTop = el.scrollHeight;
  }, []);

  // Follow the tail on new content while pinned. The typing bubble counts as
  // growth — it appears below the last message and would otherwise be cut off.
  // BEFORE paint: a post-paint scroll shows each arriving bubble below the fold
  // for one frame first.
  const growth = entries.length + (typing ? 1 : 0);
  useLayoutEffect(() => {
    if (!pinned) return;
    setSeen(entries.length);
    scrollToTail();
  }, [growth, entries.length, pinned, scrollToTail]);

  // A send re-arms the follow; the tail scroll and the unread count then fall out
  // of the effect above.
  useEffect(() => {
    setPinned(true);
  }, [pinToken]);

  // The live region stays silent until the replayed backlog has settled, so a
  // returning visitor does not have their whole stored conversation read out.
  // Deferred by a macrotask: the settling commit's own rows must be in place
  // before announcements start. A later replay patches entries where they are.
  useEffect(() => {
    if (loading || announce) return;
    const timer = setTimeout(() => setAnnounce(true), 0);
    return () => clearTimeout(timer);
  }, [loading, announce]);

  const jumpToLatest = useCallback(() => {
    scrollToTail();
    setPinned(true);
  }, [scrollToTail]);

  const unread = Math.max(0, entries.length - seen);

  return (
    <div className="tcw-transcript-frame">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="tcw-transcript"
        data-testid="transcript"
        role="log"
        aria-live={announce ? 'polite' : 'off'}
        aria-relevant="additions"
        aria-label="Conversation"
        tabIndex={0}
      >
        {loading ? (
          <div className="tcw-centered">
            <Spinner label="Loading the conversation" />
          </div>
        ) : null}
        {!loading && entries.length === 0 && !typing ? (
          <div className="tcw-centered">
            <EmptyState
              title="Start the conversation"
              description="Send a message and the reply lands right here."
            />
          </div>
        ) : null}
        {rows.map((row) =>
          row.kind === 'day' ? (
            <p key={row.key} className="tcw-day">
              <span>{row.label}</span>
            </p>
          ) : (
            <EntryRow
              key={row.key}
              row={row}
              answeredIds={answeredIds}
              locked={locked}
              onAnswer={onAnswer}
              onAnswered={onAnswered}
              onRetry={onRetry}
              onSend={onSend}
              onSubmitForm={onSubmitForm}
            />
          ),
        )}
        {typing ? <TypingBubble /> : null}
      </div>
      {!pinned ? (
        <div className="tcw-jump-wrap">
          <Button
            type="button"
            variant="secondary"
            className="tcw-jump"
            onClick={jumpToLatest}
            data-testid="jump-to-latest"
          >
            <ArrowDownIcon aria-hidden="true" />
            {unread > 0 ? `${unread} new` : 'Jump to latest'}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

interface EntryRowProps {
  readonly row: Extract<TranscriptRow, { kind: 'entry' }>;
  readonly answeredIds: ReadonlySet<string>;
  readonly locked: boolean;
  readonly onAnswer: (interactionId: string, answer: unknown) => Promise<void>;
  readonly onAnswered: () => void;
  readonly onRetry: (retryId: string) => void;
  readonly onSend: (text: string) => void;
  readonly onSubmitForm: (token: string, values: Record<string, unknown>) => Promise<void>;
}

function EntryRow({
  row,
  answeredIds,
  locked,
  onAnswer,
  onAnswered,
  onRetry,
  onSend,
  onSubmitForm,
}: EntryRowProps): ReactElement {
  const { entry, time } = row;
  const retryId = entry.kind === 'message' ? entry.retryId : null;
  return (
    <div className="tcw-group">
      {time !== null ? (
        <p
          className={
            entry.kind === 'message' && entry.direction === 'in'
              ? 'tcw-time tcw-time--in'
              : 'tcw-time'
          }
        >
          {time}
        </p>
      ) : null}
      {entry.kind === 'question' ? (
        <QuestionCard
          question={entry.question}
          answered={answeredIds.has(entry.question.interactionId)}
          onAnswer={onAnswer}
          onAnswered={onAnswered}
          locked={locked}
        />
      ) : entry.kind === 'media' ? (
        <MediaCard item={entry.item} onSend={onSend} locked={locked} />
      ) : entry.kind === 'form' ? (
        <FormCard item={entry.item} onSubmitForm={onSubmitForm} locked={locked} />
      ) : (
        <Bubble
          direction={entry.direction}
          text={entry.text}
          status={entry.status}
          error={entry.error}
          groupStart={row.groupStart}
          {...(retryId !== null ? { onRetry: () => onRetry(retryId) } : {})}
        />
      )}
    </div>
  );
}

/** The animated three-dot bubble. The dots are spans painted by the stylesheet,
 * which stands the animation down under `prefers-reduced-motion`. */
function TypingBubble(): ReactElement {
  return (
    <div className="tcw-row tcw-row--out tcw-row--start" data-testid="typing">
      <div className="tcw-bubble tcw-bubble--typing">
        <span className="tcw-dots" role="status" aria-label="Typing a reply">
          <span className="tcw-dot" />
          <span className="tcw-dot" />
          <span className="tcw-dot" />
        </span>
      </div>
    </div>
  );
}
