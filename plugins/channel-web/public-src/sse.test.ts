import { describe, expect, it } from 'vitest';

import { SseFrameParser, readSseFrames, sseOpenToken } from '@/sse';

function stream(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body);
}

describe('SseFrameParser', () => {
  it('yields a complete frame and keeps the partial tail', () => {
    const parser = new SseFrameParser();
    expect(parser.push('event: chat.message\ndata: {"a":1}\n\nevent: chat.')).toEqual([
      { event: 'chat.message', data: '{"a":1}' },
    ]);
    expect(parser.push('answered\ndata: {}\n\n')).toEqual([{ event: 'chat.answered', data: '{}' }]);
  });

  it('accepts CRLF frame separators', () => {
    const parser = new SseFrameParser();
    expect(parser.push('event: x\r\ndata: 1\r\n\r\n')).toEqual([{ event: 'x', data: '1' }]);
  });

  it('joins multi-line data and skips comment lines', () => {
    const parser = new SseFrameParser();
    expect(parser.push(': keepalive\ndata: one\ndata: two\n\n')).toEqual([
      { event: 'message', data: 'one\ntwo' },
    ]);
  });

  it('drops a frame that carries no data and no event name', () => {
    const parser = new SseFrameParser();
    expect(parser.push(': keepalive\n\n')).toEqual([]);
  });

  it('strips exactly one leading space after the colon', () => {
    const parser = new SseFrameParser();
    expect(parser.push('data:  padded\n\n')).toEqual([{ event: 'message', data: ' padded' }]);
  });

  it('treats a field with no colon as an empty value', () => {
    const parser = new SseFrameParser();
    expect(parser.push('event\ndata: x\n\n')).toEqual([{ event: '', data: 'x' }]);
  });
});

describe('sseOpenToken', () => {
  it('never repeats, so two opens can never be coalesced onto one connection', () => {
    expect(sseOpenToken()).not.toEqual(sseOpenToken());
  });
});

describe('readSseFrames', () => {
  it('reads frames across chunk boundaries', async () => {
    const frames = [];
    for await (const frame of readSseFrames(stream('event: a\nda', 'ta: 1\n\n'))) {
      frames.push(frame);
    }
    expect(frames).toEqual([{ event: 'a', data: '1' }]);
  });

  it('refuses a response with no body', async () => {
    const iterator = readSseFrames(new Response(null));
    await expect(iterator.next()).rejects.toThrow('SSE response has no body');
  });

  it('surfaces an abort rather than ending the iterator quietly', async () => {
    const controller = new AbortController();
    controller.abort();
    const iterator = readSseFrames(stream('event: a\ndata: 1\n\n'), controller.signal);
    await expect(iterator.next()).rejects.toThrow();
  });
});
