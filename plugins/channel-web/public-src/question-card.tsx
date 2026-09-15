/**
 * The inline card for one `ask_user` question: the prompt, any display media, the
 * per-format answer controls, a live countdown, and the settled-state badge.
 *
 * A question is LIVE only while it is neither answered nor past its deadline.
 * Answered is authoritative from the transcript (`chat.answered`), so a question
 * settled from another tab settles here too. The deadline is watched by a
 * SELF-RESCHEDULING timer that sleeps until the last minute and only then ticks
 * per second — a question hours out costs one timer, not one render a second. The
 * per-second text is for the eye alone; what is SPOKEN changes only at coarse
 * thresholds, so a minute of ticks cannot drown out the transcript's live region.
 *
 * Every settled state wears a badge, and a card that takes the controls away under
 * the visitor — by settling, or by sending the answer they just gave — keeps focus
 * on itself rather than dropping it to the document.
 */
import { Badge } from '@tai42/studio-sdk';
import type { ReactElement, RefObject } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';

import { MediaItems } from '@/media-card';
import { QuestionControls, type QuestionItem } from '@/question-controls';

export type { QuestionItem };

/** How long before the deadline the remaining time is spelled out. */
const COUNTDOWN_MS = 60_000;

export interface QuestionCardProps {
  readonly question: QuestionItem;
  /** Settled by a `chat.answered` frame — the durable answer state. */
  readonly answered: boolean;
  /** Sends one answer to the answer door. Rejects with the visitor-facing reason. */
  readonly onAnswer: (interactionId: string, answer: unknown) => Promise<void>;
  /** Called after an accepted answer so the page can put focus back where the
   * visitor was typing. */
  readonly onAnswered: () => void;
  /** The whole page is out of action (an ended session) — no answer can land. */
  readonly locked: boolean;
}

/** Whole seconds left, floored at zero. */
export function secondsLeft(timeoutAt: string, now: number): number {
  const deadline = Date.parse(timeoutAt);
  if (!Number.isFinite(deadline)) return 0;
  return Math.max(0, Math.ceil((deadline - now) / 1000));
}

/**
 * What a screen reader is told about the time left — COARSE, and empty outside the
 * countdown window. The visible countdown changes every second, and announcing
 * each tick would queue a minute of speech and drown out the transcript's own live
 * region; this string only changes as the remaining time crosses a threshold, and
 * an unchanged string re-renders without announcing again.
 */
export function countdownAnnouncement(remaining: number): string {
  if (remaining <= 0 || remaining >= 60) return '';
  if (remaining >= 30) return 'Less than a minute left to answer';
  if (remaining >= 10) return 'Less than 30 seconds left to answer';
  return 'Less than 10 seconds left to answer';
}

/**
 * The self-rescheduling deadline clock: one timer, rescheduled after each tick.
 * Far from the deadline it sleeps until the countdown window opens; inside it ticks
 * every second; at the deadline it stops. `active` is false for a card that can no
 * longer be answered (answered, or a locked page), so such a card runs no timer.
 */
function useDeadlineClock(timeoutAt: string, active: boolean): number {
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const left = Date.parse(timeoutAt) - now;
    if (!Number.isFinite(left) || left <= 0) return;
    const delay = left > COUNTDOWN_MS ? left - COUNTDOWN_MS : 1000;
    const timer = setTimeout(() => setNow(Date.now()), delay);
    return () => clearTimeout(timer);
  }, [active, timeoutAt, now]);
  return now;
}

/**
 * Latch focus onto the card when it stops being live under the visitor. Settling
 * takes the controls away; if the visitor was inside them, focus moves to the card
 * — which now reads out the question and the badge that replaced them — rather than
 * falling to `<body>`. Focus is LATCHED as it moves: removing the focused control
 * fires no blur, and by the time the effect runs the control is already gone.
 */
function useSettleFocus(live: boolean): {
  cardRef: RefObject<HTMLDivElement | null>;
  hadFocus: RefObject<boolean>;
} {
  const cardRef = useRef<HTMLDivElement | null>(null);
  const hadFocus = useRef(false);
  const wasLive = useRef(live);
  useEffect(() => {
    if (wasLive.current && !live && hadFocus.current) cardRef.current?.focus();
    wasLive.current = live;
  }, [live]);
  return { cardRef, hadFocus };
}

/** The settled-state badge: answered, expired, or shut out by an ended session. */
function QuestionBadges({
  answered,
  expired,
  locked,
}: {
  readonly answered: boolean;
  readonly expired: boolean;
  readonly locked: boolean;
}): ReactElement {
  return (
    <>
      {answered ? <Badge variant="success">Answered</Badge> : null}
      {expired ? <Badge variant="warning">Expired</Badge> : null}
      {!answered && !expired && locked ? <Badge variant="neutral">Session ended</Badge> : null}
    </>
  );
}

/** The countdown for a live card: the per-second visible text (for the eye, and only
 * inside the last minute) plus the coarser spoken form on its own live region. */
function QuestionCountdown({ remaining }: { readonly remaining: number }): ReactElement {
  return (
    <>
      {remaining <= COUNTDOWN_MS / 1000 ? (
        // Presentational: the per-second text is for the eye. The live region
        // below carries the spoken form, on its own coarser schedule.
        <p className="tcw-countdown" aria-hidden="true">
          {remaining === 1 ? '1 second left to answer' : `${remaining} seconds left to answer`}
        </p>
      ) : null}
      <p className="tai-visually-hidden" role="status">
        {countdownAnnouncement(remaining)}
      </p>
    </>
  );
}

export function QuestionCard({
  question,
  answered,
  onAnswer,
  onAnswered,
  locked,
}: QuestionCardProps): ReactElement {
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timeoutAt = question.timeoutAt;

  const now = useDeadlineClock(timeoutAt, !answered && !locked);
  const remaining = secondsLeft(timeoutAt, now);
  const expired = !answered && remaining === 0;
  const live = !answered && !expired && !locked;

  const { cardRef, hadFocus } = useSettleFocus(live);

  const submit = useCallback(
    (answer: unknown) => {
      // Sending disables the control the visitor is inside; the browser will not
      // leave focus on a disabled control, so it drops to the document and clears
      // the latch. Whether they were in the card is therefore read HERE, and a
      // refusal puts focus back on the card — where the alert now is — unless the
      // visitor has meanwhile put it somewhere of their own choosing.
      const held = hadFocus.current;
      setSending(true);
      setError(null);
      onAnswer(question.interactionId, answer).then(
        () => {
          setSending(false);
          setDraft('');
          onAnswered();
        },
        (err: unknown) => {
          setSending(false);
          setError(err instanceof Error ? err.message : String(err));
          if (held && document.activeElement === document.body) cardRef.current?.focus();
        },
      );
    },
    [onAnswer, onAnswered, question.interactionId, cardRef, hadFocus],
  );

  return (
    <div className="tcw-row tcw-row--out tcw-row--start">
      <div
        className="tcw-question"
        ref={cardRef}
        tabIndex={-1}
        onFocus={() => {
          hadFocus.current = true;
        }}
        onBlur={() => {
          hadFocus.current = false;
        }}
      >
        <div className="tcw-question-head">
          <p className="tcw-text">{question.question}</p>
          <QuestionBadges answered={answered} expired={expired} locked={locked} />
        </div>
        {/* Display media rides between the prompt and its controls — the same
         * component a media card uses, so a question's images/links render
         * identically. Shown in every state (answered, expired, locked): the media
         * is context for the prompt, not an answer control. */}
        {question.media !== null ? <MediaItems media={question.media} /> : null}
        {live ? (
          <QuestionControls
            question={question}
            draft={draft}
            onDraft={setDraft}
            sending={sending}
            onSubmit={submit}
          />
        ) : null}
        {live ? <QuestionCountdown remaining={remaining} /> : null}
        {error !== null ? (
          <p className="tcw-question-error" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}
