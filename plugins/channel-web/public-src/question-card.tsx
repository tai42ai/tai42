/**
 * The inline widget for one `ask_user` question, per `answer_format`:
 * a text field, a Yes/No pair, one button per option, or — for `external` — a
 * link out to the question's own callback page, which is where that format is
 * answered.
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
import type { ReactElement } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Badge, Button, ExternalLinkButton, Spinner, TextInput } from '@tai42/studio-sdk';

import type { ChatItem } from '@/use-chat-stream';

/** The transcript item this card renders. */
export type QuestionItem = Extract<ChatItem, { kind: 'question' }>;

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

export function QuestionCard({
  question,
  answered,
  onAnswer,
  onAnswered,
  locked,
}: QuestionCardProps): ReactElement {
  const [now, setNow] = useState<number>(() => Date.now());
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timeoutAt = question.timeoutAt;

  const remaining = secondsLeft(timeoutAt, now);
  const expired = !answered && remaining === 0;
  const live = !answered && !expired && !locked;

  // One timer, rescheduled after each tick: far from the deadline it sleeps until
  // the countdown window opens, inside it ticks every second, and at the deadline
  // it stops. A card that is not live shows no clock — answered, expired, or shut
  // out by an ended session — so it runs none.
  useEffect(() => {
    if (!live) return;
    const left = Date.parse(timeoutAt) - now;
    if (!Number.isFinite(left) || left <= 0) return;
    const delay = left > COUNTDOWN_MS ? left - COUNTDOWN_MS : 1000;
    const timer = setTimeout(() => setNow(Date.now()), delay);
    return () => clearTimeout(timer);
  }, [live, timeoutAt, now]);

  // Settling takes the controls away. If the visitor was inside them, focus moves
  // to the card — which now reads out the question and the badge that replaced
  // them — rather than falling to <body>. Focus is LATCHED as it moves: removing
  // the focused control fires no blur, and by the time this effect runs the
  // control is already gone from the document.
  const cardRef = useRef<HTMLDivElement | null>(null);
  const hadFocus = useRef(false);
  const wasLive = useRef(live);
  useEffect(() => {
    if (wasLive.current && !live && hadFocus.current) cardRef.current?.focus();
    wasLive.current = live;
  }, [live]);

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
    [onAnswer, onAnswered, question.interactionId],
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
          {answered ? <Badge variant="success">Answered</Badge> : null}
          {expired ? <Badge variant="warning">Expired</Badge> : null}
          {!answered && !expired && locked ? <Badge variant="neutral">Session ended</Badge> : null}
        </div>
        {live ? (
          <QuestionControls
            question={question}
            draft={draft}
            onDraft={setDraft}
            sending={sending}
            onSubmit={submit}
          />
        ) : null}
        {live && remaining <= COUNTDOWN_MS / 1000 ? (
          // Presentational: the per-second text is for the eye. The live region
          // below carries the spoken form, on its own coarser schedule.
          <p className="tcw-countdown" aria-hidden="true">
            {remaining === 1 ? '1 second left to answer' : `${remaining} seconds left to answer`}
          </p>
        ) : null}
        {live ? (
          <p className="tai-visually-hidden" role="status">
            {countdownAnnouncement(remaining)}
          </p>
        ) : null}
        {error !== null ? (
          <p className="tcw-question-error" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}

interface ControlsProps {
  readonly question: QuestionItem;
  readonly draft: string;
  readonly onDraft: (value: string) => void;
  readonly sending: boolean;
  readonly onSubmit: (answer: unknown) => void;
}

/** The per-format controls. The switch has NO default arm: a new answer format
 * has to be given a widget here before it type-checks. */
function QuestionControls(props: ControlsProps): ReactElement {
  const { question } = props;
  switch (question.answerFormat) {
    case 'text':
      return <TextAnswer {...props} />;
    case 'confirm':
      return <ConfirmAnswer {...props} />;
    case 'select':
      return <SelectAnswer {...props} options={question.options ?? []} />;
    case 'external':
      // The callback ticket reaches the page for this format alone, and the type
      // carries that: the stream admits an `external` question only when it
      // carries one. An unusable URL is neutralized into plain text by
      // `ExternalLinkButton`.
      return (
        <div className="tcw-question-actions">
          <ExternalLinkButton url={question.callbackUrl}>Open to answer</ExternalLinkButton>
        </div>
      );
  }
}

function TextAnswer({ question, draft, onDraft, sending, onSubmit }: ControlsProps): ReactElement {
  const blank = draft.trim() === '';
  return (
    <form
      className="tcw-question-actions"
      onSubmit={(event) => {
        event.preventDefault();
        if (!blank && !sending) onSubmit(draft.trim());
      }}
    >
      <TextInput
        value={draft}
        onChange={(event) => onDraft(event.target.value)}
        aria-label={question.question}
        placeholder="Type your answer"
        disabled={sending}
      />
      <Button type="submit" variant="primary" disabled={blank || sending}>
        {sending ? <Spinner label="Sending your answer" /> : 'Answer'}
      </Button>
    </form>
  );
}

function ConfirmAnswer({ sending, onSubmit }: ControlsProps): ReactElement {
  return (
    <div className="tcw-question-actions">
      <Button type="button" variant="primary" disabled={sending} onClick={() => onSubmit(true)}>
        Yes
      </Button>
      <Button type="button" variant="secondary" disabled={sending} onClick={() => onSubmit(false)}>
        No
      </Button>
      {sending ? <Spinner label="Sending your answer" /> : null}
    </div>
  );
}

function SelectAnswer({
  options,
  sending,
  onSubmit,
}: ControlsProps & { readonly options: readonly string[] }): ReactElement {
  return (
    <div className="tcw-question-actions">
      {options.map((option) => (
        <Button
          key={option}
          type="button"
          variant="secondary"
          disabled={sending}
          onClick={() => onSubmit(option)}
        >
          {option}
        </Button>
      ))}
      {sending ? <Spinner label="Sending your answer" /> : null}
    </div>
  );
}
