import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  QuestionCard,
  countdownAnnouncement,
  secondsLeft,
  type QuestionItem,
} from '@/question-card';
import type { AnswerFormat, JsonSchema } from '@/use-chat-stream';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const CALLBACK = 'https://app.example/api/interactions/callback/ticket-1';

/** The clock the timer tests run on. Both the fake timers and `Date.now` are
 * pinned here, so a second on this card's clock is a second the test advanced —
 * never wall time the test box happened to spend elsewhere. */
const T0 = Date.parse('2026-08-07T12:00:00Z');

function freezeClock(): void {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
}

/** The default answer schema a `form` question renders with. */
const FORM_SCHEMA: JsonSchema = {
  type: 'object',
  properties: { note: { type: 'string' } },
};

/** The format-dependent extras are the format's own, so they are not something an
 * override can change: only `external` reaches the page with a callback ticket, and
 * only `form` with an answer schema. */
function question(
  format: AnswerFormat,
  overrides: Partial<Omit<QuestionItem, 'answerFormat' | 'callbackUrl' | 'schema'>> = {},
): QuestionItem {
  const base = {
    kind: 'question',
    id: 'q1',
    interactionId: 'int-1',
    question: 'Deploy to production?',
    options: format === 'select' ? ['Now', 'Tonight'] : null,
    timeoutAt: new Date(Date.now() + 10 * 60_000).toISOString(),
    ts: new Date().toISOString(),
    ...overrides,
  } as const;
  if (format === 'external')
    return { ...base, answerFormat: format, callbackUrl: CALLBACK, schema: null };
  if (format === 'form')
    return { ...base, answerFormat: format, callbackUrl: null, schema: FORM_SCHEMA };
  return { ...base, answerFormat: format, callbackUrl: null, schema: null };
}

/** An `external` question pointing at `url` — the one format whose widget opens
 * the ticket it was given. */
function externalQuestion(url: string): QuestionItem {
  return { ...question('external'), answerFormat: 'external', callbackUrl: url, schema: null };
}

/** One turn of the card's self-rescheduling clock. React commits the tick's state
 * update when the act scope closes, so the NEXT timer is only armed by then —
 * one scope per tick. */
async function tick(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** `seconds` turns of the per-second clock — one act scope each, so every tick
 * arms the next. */
async function tickSeconds(seconds: number): Promise<void> {
  for (let second = 0; second < seconds; second += 1) await tick(1_000);
}

function renderCard(item: QuestionItem, overrides = {}) {
  const onAnswer = vi.fn().mockResolvedValue(undefined);
  const onAnswered = vi.fn();
  render(
    <QuestionCard
      question={item}
      answered={false}
      onAnswer={onAnswer}
      onAnswered={onAnswered}
      locked={false}
      {...overrides}
    />,
  );
  return { onAnswer, onAnswered };
}

describe('secondsLeft', () => {
  it('counts whole seconds and floors at zero', () => {
    const now = Date.parse('2026-08-07T12:00:00Z');
    expect(secondsLeft('2026-08-07T12:00:30Z', now)).toBe(30);
    expect(secondsLeft('2026-08-07T11:59:00Z', now)).toBe(0);
  });

  it('treats an unparsable deadline as already past', () => {
    expect(secondsLeft('soon', Date.now())).toBe(0);
  });
});

describe('countdownAnnouncement', () => {
  it('speaks in coarse steps, and says nothing outside the countdown window', () => {
    expect(countdownAnnouncement(61)).toBe('');
    expect(countdownAnnouncement(60)).toBe('');
    expect(countdownAnnouncement(59)).toBe('Less than a minute left to answer');
    expect(countdownAnnouncement(30)).toBe('Less than a minute left to answer');
    expect(countdownAnnouncement(29)).toBe('Less than 30 seconds left to answer');
    expect(countdownAnnouncement(10)).toBe('Less than 30 seconds left to answer');
    expect(countdownAnnouncement(9)).toBe('Less than 10 seconds left to answer');
    expect(countdownAnnouncement(0)).toBe('');
  });

  it('changes three times across the last minute, not sixty', () => {
    let changes = 0;
    let previous = countdownAnnouncement(60);
    for (let remaining = 59; remaining > 0; remaining -= 1) {
      const spoken = countdownAnnouncement(remaining);
      if (spoken !== previous) changes += 1;
      previous = spoken;
    }

    expect(changes).toBe(3);
  });
});

describe('QuestionCard', () => {
  it('sends a typed answer for the text format', async () => {
    const user = userEvent.setup();
    const { onAnswer, onAnswered } = renderCard(question('text'));

    await user.type(screen.getByRole('textbox'), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
    expect(onAnswer).toHaveBeenCalledWith('int-1', 'ship it');
  });

  it('refuses to send a blank text answer', () => {
    renderCard(question('text'));

    expect(screen.getByRole('button', { name: 'Answer' })).toBeDisabled();
  });

  it('sends a boolean for the confirm format', async () => {
    const user = userEvent.setup();
    const { onAnswer, onAnswered } = renderCard(question('confirm'));

    await user.click(screen.getByRole('button', { name: 'No' }));

    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
    expect(onAnswer).toHaveBeenCalledWith('int-1', false);
  });

  it('offers one button per option and sends the chosen one', async () => {
    const user = userEvent.setup();
    const { onAnswer, onAnswered } = renderCard(question('select'));

    await user.click(screen.getByRole('button', { name: 'Tonight' }));

    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
    expect(onAnswer).toHaveBeenCalledWith('int-1', 'Tonight');
  });

  it('sends an external question out to its own callback page', () => {
    renderCard(question('external'));

    expect(screen.getByRole('link', { name: /open to answer/i })).toHaveAttribute('href', CALLBACK);
  });

  it('neutralizes an external question whose ticket is not a usable link', () => {
    // Not an absolute http(s) URL: opening it would be a same-origin in-app
    // navigation nobody intended, so the link is rendered as plain text instead.
    renderCard(externalQuestion('/api/interactions/callback/ticket-1'));

    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByText('Open to answer')).toBeInTheDocument();
  });

  it('builds and sends the object for the schema-driven form format', async () => {
    const user = userEvent.setup();
    const { onAnswer, onAnswered } = renderCard(question('form'));

    await user.type(screen.getByRole('textbox'), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Answer' }));

    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
    expect(onAnswer).toHaveBeenCalledWith('int-1', { note: 'ship it' });
  });

  it('shows a loud notice for a form whose schema is not an object, never a dropped control', () => {
    // The stream already rejects a non-object schema, so this is the defensive last
    // line — a schema that slips through non-object is a visible alert, not silence.
    const malformed = { ...question('form'), schema: null } as unknown as QuestionItem;
    renderCard(malformed);

    expect(screen.getByRole('alert')).toHaveTextContent(/malformed/i);
  });

  it('settles into a badge with no controls once answered', () => {
    renderCard(question('confirm'), { answered: true });

    expect(screen.getByText('Answered')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Yes' })).not.toBeInTheDocument();
  });

  it('settles into a badge with no controls once the deadline has passed', () => {
    renderCard(question('confirm', { timeoutAt: new Date(Date.now() - 1000).toISOString() }));

    expect(screen.getByText('Expired')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Yes' })).not.toBeInTheDocument();
  });

  it('takes no answer while the page is locked out, and says why', () => {
    renderCard(question('confirm'), { locked: true });

    expect(screen.queryByRole('button', { name: 'Yes' })).not.toBeInTheDocument();
    expect(screen.getByText('Session ended')).toBeInTheDocument();
  });

  it('runs no clock for a card the visitor can no longer answer', () => {
    freezeClock();
    // Inside the countdown window, where a live card ticks once a second.
    renderCard(question('confirm', { timeoutAt: new Date(T0 + 30_000).toISOString() }), {
      locked: true,
    });

    // Nothing on a locked card reads the clock, so a card that kept ticking would
    // be re-rendering thirty times for a badge that cannot change.
    expect(screen.queryByText(/left to answer/)).not.toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('spells the remaining time out in the last minute only', () => {
    freezeClock();
    renderCard(question('confirm', { timeoutAt: new Date(T0 + 30_000).toISOString() }));

    expect(screen.getByText('30 seconds left to answer')).toBeInTheDocument();
  });

  it('says nothing about the clock while the deadline is far off', () => {
    freezeClock();
    renderCard(question('confirm', { timeoutAt: new Date(T0 + 10 * 60_000).toISOString() }));

    expect(screen.queryByText(/left to answer/)).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('');
  });

  it('sleeps until the countdown window, then counts the seconds down', async () => {
    freezeClock();
    renderCard(question('confirm', { timeoutAt: new Date(T0 + 90_000).toISOString() }));

    expect(screen.queryByText(/left to answer/)).not.toBeInTheDocument();

    await tick(30_000);
    expect(screen.getByText('60 seconds left to answer')).toBeInTheDocument();

    await tick(1_000);
    expect(screen.getByText('59 seconds left to answer')).toBeInTheDocument();
  });

  it('counts down for the eye, but speaks only when the time crosses a threshold', async () => {
    freezeClock();
    renderCard(question('confirm', { timeoutAt: new Date(T0 + 45_000).toISOString() }));

    // A screen reader that heard every tick would queue a minute of speech and
    // drown out the transcript's own live region, so the per-second text is
    // presentational and the spoken form is coarse.
    expect(screen.getByText('45 seconds left to answer')).toHaveAttribute('aria-hidden', 'true');
    const spoken = screen.getByRole('status');
    expect(spoken).toHaveTextContent('Less than a minute left to answer');

    await tickSeconds(1);
    expect(screen.getByText('44 seconds left to answer')).toBeInTheDocument();
    expect(spoken).toHaveTextContent('Less than a minute left to answer');

    await tickSeconds(15);
    expect(screen.getByText('29 seconds left to answer')).toBeInTheDocument();
    expect(spoken).toHaveTextContent('Less than 30 seconds left to answer');
  });

  it('expires under the visitor without dropping their focus to the page', async () => {
    freezeClock();
    renderCard(question('confirm', { timeoutAt: new Date(T0 + 5_000).toISOString() }));
    const yes = screen.getByRole('button', { name: 'Yes' });
    yes.focus();
    expect(yes).toHaveFocus();

    await tickSeconds(5);

    expect(screen.getByText('Expired')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Yes' })).not.toBeInTheDocument();
    expect(document.activeElement).toBe(document.querySelector('.tcw-question'));
  });

  it('shows the refusal on the card and keeps the controls answerable', async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn().mockRejectedValue(new Error('That question was already answered.'));
    render(
      <QuestionCard
        question={question('confirm')}
        answered={false}
        onAnswer={onAnswer}
        onAnswered={vi.fn()}
        locked={false}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'Yes' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'That question was already answered.',
    );
    expect(screen.getByRole('button', { name: 'Yes' })).toBeEnabled();
  });

  it('keeps focus on the card when the answer is refused', async () => {
    const user = userEvent.setup();
    let refuse = (_err: unknown): void => {};
    const onAnswer = vi.fn().mockReturnValue(
      new Promise<void>((_resolve, reject) => {
        refuse = reject;
      }),
    );
    render(
      <QuestionCard
        question={question('confirm')}
        answered={false}
        onAnswer={onAnswer}
        onAnswered={vi.fn()}
        locked={false}
      />,
    );
    const card = document.querySelector('.tcw-question');

    await user.click(screen.getByRole('button', { name: 'Yes' }));

    // Sending disables the control the visitor is inside. A browser will not leave
    // focus on a disabled control: it drops to the document and fires the focusout
    // that clears the card's own latch. jsdom implements no such fixup, so both
    // halves are staged by hand (the tabindex is what lets jsdom focus <body>).
    act(() => {
      const yes = screen.getByRole('button', { name: 'Yes' });
      document.body.tabIndex = -1;
      document.body.focus();
      document.body.removeAttribute('tabindex');
      fireEvent.focusOut(yes);
    });
    expect(document.body).toHaveFocus();

    await act(async () => {
      refuse(new Error('That question was already answered.'));
    });

    expect(screen.getByRole('alert')).toHaveTextContent('That question was already answered.');
    expect(card).toHaveFocus();
  });
});
