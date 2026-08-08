import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatApiError } from '@/api';
import { reconnectDelayMs, useChatStream } from '@/use-chat-stream';

const api = vi.hoisted(() => ({ openChatStream: vi.fn() }));

vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...api,
}));

const TS = '2026-08-07T10:00:00+00:00';

function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

const HELLO = frame('chat.message', { id: 'm1', direction: 'out', text: 'hi', ts: TS });
const BACKLOG_DONE = frame('chat.backlog_done', {});

/** A stream that delivers the given text and then ENDS, which is what makes the
 * hook fall through to its reconnect. */
function closing(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
  );
}

/** A stream that stays open — the healthy steady state, and what a follow-up
 * connection is given so the loop parks instead of spinning. */
function open(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      },
    }),
  );
}

/** A stream the test feeds one frame at a time, so a state the hook passes
 * THROUGH can be observed before the next frame moves it on. */
function pushable(): { response: Response; push: (chunk: string) => void } {
  const encoder = new TextEncoder();
  let sink!: ReadableStreamDefaultController<Uint8Array>;
  const response = new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        sink = controller;
      },
    }),
  );
  return { response, push: (chunk: string) => sink.enqueue(encoder.encode(chunk)) };
}

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

describe('useChatStream', () => {
  it('publishes the replayed backlog and marks it loaded', async () => {
    api.openChatStream.mockResolvedValue(open(HELLO, BACKLOG_DONE));

    const { result } = renderHook(() => useChatStream('site-alpha', 0));

    await waitFor(() => expect(result.current.backlogLoaded).toBe(true));
    expect(result.current.connected).toBe(true);
    expect(result.current.items).toHaveLength(1);
  });

  it('reconnects when the stream ends, and does not duplicate the replayed entry', async () => {
    api.openChatStream
      .mockResolvedValueOnce(closing(HELLO, BACKLOG_DONE))
      .mockResolvedValue(open(HELLO, BACKLOG_DONE));

    const { result } = renderHook(() => useChatStream('site-alpha', 0));

    await waitFor(() => expect(api.openChatStream).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.backlogLoaded).toBe(true));
    expect(result.current.items).toHaveLength(1);
  });

  it('stops for good when the session is gone — only a reload can recover', async () => {
    api.openChatStream.mockRejectedValue(
      new ChatApiError('Your chat session ended.', 401, 'session_missing'),
    );

    const { result } = renderHook(() => useChatStream('site-alpha', 0));

    await waitFor(() => expect(result.current.sessionExpired).toBe(true));
    expect(api.openChatStream).toHaveBeenCalledTimes(1);
  });

  it('stops for good when the deployment runs no transcript store', async () => {
    api.openChatStream.mockRejectedValue(
      new ChatApiError('store off', 501, 'web_transcript_store_off'),
    );

    const { result } = renderHook(() => useChatStream('site-alpha', 0));

    await waitFor(() => expect(result.current.disabled).toBe(true));
    expect(api.openChatStream).toHaveBeenCalledTimes(1);
  });

  it('surfaces a malformed frame instead of rendering a blank entry', async () => {
    api.openChatStream.mockResolvedValue(open(frame('chat.message', { id: 'm1' })));

    const { result } = renderHook(() => useChatStream('site-alpha', 0));

    await waitFor(() => expect(result.current.error?.message).toMatch(/malformed/));
    expect(result.current.items).toHaveLength(0);
  });

  it('clears the malformed-frame error on the next good frame', async () => {
    const feed = pushable();
    api.openChatStream.mockResolvedValue(feed.response);

    const { result } = renderHook(() => useChatStream('site-alpha', 0));
    await waitFor(() => expect(result.current.connected).toBe(true));

    feed.push(frame('chat.message', { id: 'm1' }));
    await waitFor(() => expect(result.current.error?.message).toMatch(/malformed/));

    // One bad entry on a healthy stream must not leave the page wearing "part of
    // this conversation couldn't be shown" for the life of the tab.
    feed.push(HELLO);
    await waitFor(() => expect(result.current.error).toBeNull());
    expect(result.current.items).toHaveLength(1);
  });

  it('backs off and retries after an ordinary connection failure', async () => {
    api.openChatStream
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValue(open(HELLO, BACKLOG_DONE));

    const { result } = renderHook(() => useChatStream('site-alpha', 0));

    await waitFor(() => expect(result.current.backlogLoaded).toBe(true));
    expect(api.openChatStream).toHaveBeenCalledTimes(2);
  });

  it('starts from an empty transcript when the session is rotated', async () => {
    api.openChatStream.mockResolvedValue(open(HELLO, BACKLOG_DONE));
    const { result, rerender } = renderHook(({ epoch }) => useChatStream('site-alpha', epoch), {
      initialProps: { epoch: 0 },
    });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    api.openChatStream.mockResolvedValue(open(BACKLOG_DONE));
    rerender({ epoch: 1 });

    await waitFor(() => expect(result.current.backlogLoaded).toBe(true));
    expect(result.current.items).toHaveLength(0);
  });
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
