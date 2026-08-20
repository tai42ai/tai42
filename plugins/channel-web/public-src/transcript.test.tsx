import type { ReactElement } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  GROUP_GAP_MS,
  Transcript,
  buildRows,
  dayLabel,
  isAtBottom,
  startOfNextDay,
  type TranscriptEntry,
  type TranscriptProps,
} from '@/transcript';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const DAY = 86_400_000;
const NOW = Date.parse('2026-08-07T12:00:00Z');

function said(
  key: string,
  at: number,
  direction: 'in' | 'out' = 'in',
  text = key,
): TranscriptEntry {
  return {
    kind: 'message',
    key,
    direction,
    text,
    ts: new Date(at).toISOString(),
    status: null,
    error: null,
    retryId: null,
  };
}

describe('isAtBottom', () => {
  it('is true at the edge and within the slack', () => {
    expect(isAtBottom({ scrollHeight: 1000, scrollTop: 900, clientHeight: 100 })).toBe(true);
    expect(isAtBottom({ scrollHeight: 1000, scrollTop: 880, clientHeight: 100 })).toBe(true);
  });

  it('is false once the reader has scrolled past the slack', () => {
    expect(isAtBottom({ scrollHeight: 1000, scrollTop: 500, clientHeight: 100 })).toBe(false);
  });

  it('honours a caller-supplied slack', () => {
    expect(isAtBottom({ scrollHeight: 1000, scrollTop: 800, clientHeight: 100 }, 200)).toBe(true);
  });
});

describe('dayLabel', () => {
  it('names today and yesterday', () => {
    expect(dayLabel(NOW, NOW)).toBe('Today');
    expect(dayLabel(NOW - DAY, NOW)).toBe('Yesterday');
  });

  it('writes the date out for anything older', () => {
    expect(dayLabel(NOW - 5 * DAY, NOW)).not.toMatch(/today|yesterday/i);
  });
});

describe('startOfNextDay', () => {
  it('lands on the next local midnight', () => {
    const lateNight = new Date(2026, 7, 7, 23, 59, 30).getTime();

    const midnight = startOfNextDay(lateNight);

    expect(midnight - lateNight).toBe(30_000);
    expect(new Date(midnight).getHours()).toBe(0);
  });
});

describe('buildRows', () => {
  it('opens each day with a divider', () => {
    const rows = buildRows([said('a', NOW - DAY), said('b', NOW)], NOW);

    expect(rows.filter((row) => row.kind === 'day').map((row) => row.label)).toEqual([
      'Yesterday',
      'Today',
    ]);
  });

  it('keeps one group — and one time stamp — for messages sent close together', () => {
    const rows = buildRows([said('a', NOW - 60_000), said('b', NOW)], NOW);
    const entries = rows.filter((row) => row.kind === 'entry');

    expect(entries.map((row) => row.groupStart)).toEqual([true, false]);
    expect(entries[1]?.time).toBeNull();
  });

  it('starts a new group after a long silence', () => {
    const rows = buildRows([said('a', NOW - GROUP_GAP_MS), said('b', NOW)], NOW);
    const entries = rows.filter((row) => row.kind === 'entry');

    expect(entries.map((row) => row.groupStart)).toEqual([true, true]);
  });

  it('starts a new group when the speaker changes', () => {
    const rows = buildRows([said('a', NOW, 'in'), said('b', NOW, 'out')], NOW);
    const entries = rows.filter((row) => row.kind === 'entry');

    expect(entries.map((row) => row.groupStart)).toEqual([true, true]);
  });
});

const noop = (): void => {};
const answer = async (): Promise<void> => {};

function transcript(
  entries: readonly TranscriptEntry[],
  overrides: Partial<TranscriptProps> = {},
): ReactElement {
  return (
    <Transcript
      entries={entries}
      answeredIds={new Set()}
      typing={false}
      loading={false}
      locked={false}
      onAnswer={answer}
      onAnswered={noop}
      onRetry={noop}
      onSend={noop}
      pinToken={0}
      {...overrides}
    />
  );
}

function renderTranscript(
  entries: readonly TranscriptEntry[],
  overrides: Partial<TranscriptProps> = {},
) {
  return render(transcript(entries, overrides));
}

/** jsdom measures nothing, so the region's geometry is declared outright — the
 * assertions are about which affordance appears, never a pixel. */
function setGeometry(el: HTMLElement, scrollHeight: number, clientHeight: number): void {
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true });
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true });
}

describe('Transcript', () => {
  it('shows the opener until there is something to read', () => {
    renderTranscript([]);

    expect(screen.getByText('Start the conversation')).toBeInTheDocument();
  });

  it('waits on the backlog rather than claiming the conversation is empty', () => {
    renderTranscript([], { loading: true });

    expect(screen.queryByText('Start the conversation')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Loading the conversation')).toBeInTheDocument();
  });

  it('says nothing while the replayed backlog lands, then announces politely', async () => {
    const { rerender } = renderTranscript([], { loading: true });

    expect(screen.getByTestId('transcript')).toHaveAttribute('aria-live', 'off');

    // The backlog settles: a returning visitor's stored conversation arrives in
    // this commit and must not be read out.
    rerender(transcript([said('a', NOW), said('b', NOW)]));
    expect(screen.getByTestId('transcript')).toHaveAttribute('aria-live', 'off');

    await waitFor(() =>
      expect(screen.getByTestId('transcript')).toHaveAttribute('aria-live', 'polite'),
    );
  });

  it('re-dates its day dividers on a page left open across midnight', async () => {
    vi.useFakeTimers();
    const lateNight = new Date(2026, 7, 7, 23, 59, 30).getTime();
    vi.setSystemTime(lateNight);
    renderTranscript([said('a', lateNight)]);

    expect(screen.getByText('Today')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    // Nothing arrived and nothing was scrolled: the divider has to stop calling
    // last night "Today" on its own.
    expect(screen.getByText('Yesterday')).toBeInTheDocument();
    expect(screen.queryByText('Today')).not.toBeInTheDocument();
  });

  it('shows the typing bubble on request', () => {
    renderTranscript([said('a', NOW)], { typing: true });

    expect(screen.getByLabelText('Typing a reply')).toBeInTheDocument();
  });

  it('offers jump-to-latest once the reader scrolls away, counting what they miss', async () => {
    const user = userEvent.setup();
    const { rerender } = renderTranscript([said('a', NOW), said('b', NOW)]);
    const region = screen.getByTestId('transcript');

    setGeometry(region, 1000, 100);
    region.scrollTop = 0;
    fireEvent.scroll(region);

    expect(screen.getByTestId('jump-to-latest')).toHaveTextContent('Jump to latest');

    rerender(transcript([said('a', NOW), said('b', NOW), said('c', NOW), said('d', NOW)]));
    expect(screen.getByTestId('jump-to-latest')).toHaveTextContent('2 new');

    await user.click(screen.getByTestId('jump-to-latest'));
    expect(screen.queryByTestId('jump-to-latest')).not.toBeInTheDocument();
    expect(region.scrollTop).toBe(1000);
  });

  it('returns a detached reader to the tail on their own send, unread-free', () => {
    const { rerender } = renderTranscript([said('a', NOW)]);
    const region = screen.getByTestId('transcript');

    setGeometry(region, 1000, 100);
    region.scrollTop = 0;
    fireEvent.scroll(region);
    expect(screen.getByTestId('jump-to-latest')).toBeInTheDocument();

    rerender(transcript([said('a', NOW), said('mine', NOW)], { pinToken: 1 }));

    expect(screen.queryByTestId('jump-to-latest')).not.toBeInTheDocument();
    expect(region.scrollTop).toBe(1000);
  });

  it('retries the failed send it is asked about', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    renderTranscript(
      [
        {
          kind: 'message',
          key: 'local-1',
          direction: 'in',
          text: 'hello',
          ts: new Date(NOW).toISOString(),
          status: 'failed',
          error: 'That did not send.',
          retryId: 'local-1',
        },
      ],
      { onRetry },
    );

    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(onRetry).toHaveBeenCalledWith('local-1');
  });

  it('renders a media card entry in order and wires the send door to its chips', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    const media: TranscriptEntry = {
      kind: 'media',
      key: 'md1',
      ts: new Date(NOW).toISOString(),
      item: {
        kind: 'media',
        id: 'md1',
        text: 'Here you go',
        media: [{ kind: 'image', url: 'https://example.com/a.png', caption: 'Item A' }],
        options: ['See all'],
        ts: new Date(NOW).toISOString(),
      },
    };
    renderTranscript([media], { onSend });

    expect(screen.getByRole('img', { name: 'Item A' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'See all' }));
    expect(onSend).toHaveBeenCalledWith('See all');
  });

  it('renders a media card chip disabled once the session is locked', () => {
    const media: TranscriptEntry = {
      kind: 'media',
      key: 'md1',
      ts: new Date(NOW).toISOString(),
      item: {
        kind: 'media',
        id: 'md1',
        text: 'Here you go',
        media: null,
        options: ['See all'],
        ts: new Date(NOW).toISOString(),
      },
    };
    renderTranscript([media], { locked: true });

    expect(screen.getByRole('button', { name: 'See all' })).toBeDisabled();
  });
});
