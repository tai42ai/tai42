import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatApiError } from '@/api';
import { BACKLOG_DONE, closing, frame, HELLO, open, pushable } from '@/stream-driver.test-support';
import { useChatStream } from '@/use-chat-stream';

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
