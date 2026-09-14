import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useChatStream } from '@/use-chat-stream';
import { reconnectDelayMs } from '@/transcript-subscription';
import { BACKLOG_DONE, HELLO, closing } from '@/stream-driver.test-support';

const api = vi.hoisted(() => ({ openChatStream: vi.fn() }));

vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...api,
}));

beforeEach(() => {
  // Full-jitter backoff with the jitter pinned to zero: the reconnect is then
  // immediate and the test needs no clock.
  vi.spyOn(Math, 'random').mockReturnValue(0);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  api.openChatStream.mockReset();
});

describe('the reconnect backoff', () => {
  /** Let the effect's first connection open and its stream drain. */
  async function settle(): Promise<void> {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  }

  async function waitMs(ms: number): Promise<void> {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it('is full jitter over a doubling ceiling, capped', () => {
    expect(reconnectDelayMs(0, 1)).toBe(1500);
    expect(reconnectDelayMs(1, 1)).toBe(3000);
    expect(reconnectDelayMs(4, 1)).toBe(24000);
    // The ceiling stops doubling at the cap, and stays there however long the
    // outage runs.
    expect(reconnectDelayMs(5, 1)).toBe(30000);
    expect(reconnectDelayMs(40, 1)).toBe(30000);
    // The delay is a draw from [0, ceiling), not the ceiling itself.
    expect(reconnectDelayMs(0, 0.5)).toBe(750);
    expect(reconnectDelayMs(9, 0)).toBe(0);
  });

  it('waits longer after each connection that never proved healthy', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    // Accepted, then dropped without ever replaying a backlog — an open on its
    // own proves nothing, so the attempt counter must keep climbing.
    api.openChatStream.mockImplementation(async () => closing(HELLO));

    renderHook(() => useChatStream('site-alpha', 0));
    await settle();
    expect(api.openChatStream).toHaveBeenCalledTimes(1);

    await waitMs(600);
    expect(api.openChatStream).toHaveBeenCalledTimes(1);
    await waitMs(300);
    expect(api.openChatStream).toHaveBeenCalledTimes(2);

    // 0.5 * 3000 this time. A counter reset on the bare open above would have
    // re-opened at 750ms — that bug tight-loops reconnects from every open tab.
    await waitMs(1000);
    expect(api.openChatStream).toHaveBeenCalledTimes(2);
    await waitMs(800);
    expect(api.openChatStream).toHaveBeenCalledTimes(3);
  });

  it('resets the wait only once a connection has proved healthy', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    // Every connection replays the backlog before it ends, so every reconnect
    // starts from the first step of the ladder.
    api.openChatStream.mockImplementation(async () => closing(HELLO, BACKLOG_DONE));

    renderHook(() => useChatStream('site-alpha', 0));
    await settle();
    expect(api.openChatStream).toHaveBeenCalledTimes(1);

    await waitMs(900);
    expect(api.openChatStream).toHaveBeenCalledTimes(2);
    await waitMs(900);
    expect(api.openChatStream).toHaveBeenCalledTimes(3);
    await waitMs(900);
    expect(api.openChatStream).toHaveBeenCalledTimes(4);
  });
});
