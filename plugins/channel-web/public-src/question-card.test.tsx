import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { countdownAnnouncement, QuestionCard, secondsLeft } from '@/question-card';
import {
  freezeClock,
  question,
  renderCard,
  T0,
  tick,
  tickSeconds,
} from '@/question-card.test-support';
import type { MediaItem } from '@/transcript-model';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

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
  it('renders a question image and link through the shared media card', () => {
    const media: MediaItem[] = [
      { kind: 'image', url: 'https://example.com/a.png', caption: 'A shot', filename: null },
      { kind: 'link', url: 'https://docs.example/p', caption: 'The doc', filename: null },
    ];
    renderCard(question('text', { media }));

    const img = screen.getByRole('img', { name: 'A shot' });
    expect(img).toHaveAttribute('src', 'https://example.com/a.png');
    const anchor = screen.getByRole('link', { name: 'The doc' });
    expect(anchor).toHaveAttribute('href', 'https://docs.example/p');
    expect(anchor).toHaveAttribute('rel', 'noreferrer noopener');
    // The prompt's own text field is still there — media is context, not a control.
    expect(screen.getByRole('textbox')).toBeInTheDocument();
  });

  it('shows a question card image even after the question is answered', () => {
    renderCard(
      question('confirm', {
        media: [{ kind: 'image', url: 'https://example.com/a.png', caption: 'A', filename: null }],
      }),
      {
        answered: true,
      },
    );

    // The controls are gone, but the media stays — it is context for the prompt.
    expect(screen.getByRole('img', { name: 'A' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Yes' })).not.toBeInTheDocument();
  });

  it('renders no media block for a question that carries none', () => {
    const { container } = render(
      <QuestionCard
        question={question('text')}
        answered={false}
        onAnswer={vi.fn().mockResolvedValue(undefined)}
        onAnswered={vi.fn()}
        locked={false}
      />,
    );

    expect(container.querySelector('.tcw-media-items')).toBeNull();
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
