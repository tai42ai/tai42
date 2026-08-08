import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ChatApiError,
  answerQuestion,
  isSessionMissing,
  isStoreOff,
  openChatStream,
  rotateSession,
  sendMessage,
} from '@/api';

const fetchMock = vi.fn();

/** One composed message's idempotency key, in the shape the door's contract
 * accepts (`^[A-Za-z0-9_-]{8,64}$`). */
const KEY = 'c0ffee-cafe_1234';

function reply(status: number, body: unknown): Response {
  return new Response(body === undefined ? '' : JSON.stringify(body), { status });
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  // The door's own wording is logged, never rendered — keep it out of the report.
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  fetchMock.mockReset();
});

describe('sendMessage', () => {
  it('posts the envelope body and returns the bridge message id', async () => {
    fetchMock.mockResolvedValue(reply(200, { data: { message_id: 'msg-1' } }));

    await expect(sendMessage('site-alpha', 'hello', KEY)).resolves.toBe('msg-1');

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/channels/web/messages');
    expect(init.method).toBe('POST');
    // The idempotency key rides the body: the door derives this delivery's
    // provider message id from it, so a re-send of a lost response is deduped
    // rather than delivered twice.
    expect(init.body).toBe(
      JSON.stringify({ identity: 'site-alpha', text: 'hello', client_message_id: KEY }),
    );
  });

  it('carries no credential of its own — the session cookie rides the request', async () => {
    fetchMock.mockResolvedValue(reply(200, { data: { message_id: 'msg-1' } }));

    await sendMessage('site-alpha', 'hello', KEY);

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(Object.keys(headers).map((key) => key.toLowerCase())).toEqual([
      'accept',
      'content-type',
    ]);
  });

  it('surfaces a lost session as a coded error the page can act on', async () => {
    fetchMock.mockResolvedValue(
      reply(401, { error: 'no visitor session', code: 'session_missing' }),
    );

    const error = await sendMessage('site-alpha', 'hi', KEY).catch((err: unknown) => err);

    expect(error).toBeInstanceOf(ChatApiError);
    expect(isSessionMissing(error)).toBe(true);
    expect((error as ChatApiError).message).toMatch(/reload the page/i);
  });

  it.each([
    [404, /no one listening/i],
    [429, /a lot of messages/i],
    [503, /a lot of messages/i],
  ])('turns HTTP %i into plain-language copy', async (status, expected) => {
    fetchMock.mockResolvedValue(reply(status, { error: 'ThreadQueueOverflowError(...)' }));

    const error = await sendMessage('site-alpha', 'hi', KEY).catch((err: unknown) => err);

    expect((error as ChatApiError).message).toMatch(expected);
    expect((error as ChatApiError).message).not.toContain('ThreadQueueOverflow');
  });

  it('keeps the door wording for a rejected body, which is already specific', async () => {
    fetchMock.mockResolvedValue(reply(400, { error: 'message text is blank' }));

    const error = await sendMessage('site-alpha', ' ', KEY).catch((err: unknown) => err);

    expect((error as ChatApiError).message).toBe('message text is blank');
  });

  it('says a body the door refused at the size cap is too long, not that it failed', async () => {
    // The body cap is checked before any field rule and before any parse, so the
    // same bytes are refused every time: the copy names the one thing that changes
    // the outcome rather than promising a retry that cannot work.
    fetchMock.mockResolvedValue(reply(413, { error: 'request body is too large' }));

    const error = (await sendMessage('site-alpha', 'x'.repeat(99), KEY).catch(
      (err: unknown) => err,
    )) as ChatApiError;

    expect(error.message).toBe('That message is too long to send — try again with a shorter one.');
    expect(error.message).not.toMatch(/something went wrong/i);
  });

  it('never puts the door field-level wording in front of the visitor', async () => {
    const detail = 'invalid request body: text: String should have at most 8000 characters';
    fetchMock.mockResolvedValue(reply(422, { error: detail }));

    const error = (await sendMessage('site-alpha', 'x'.repeat(99), KEY).catch(
      (err: unknown) => err,
    )) as ChatApiError;

    expect(error.message).toBe("That message couldn't be sent.");
    expect(error.message).not.toMatch(/String should have|invalid request body/);
    // Dropped from the transcript, not lost: the raw diagnostic stays in the log.
    expect(vi.mocked(console.error)).toHaveBeenCalledWith(
      'sending a message refused: HTTP 422',
      detail,
    );
  });

  it('refuses a success body that carries no message id', async () => {
    fetchMock.mockResolvedValue(reply(200, { data: {} }));

    await expect(sendMessage('site-alpha', 'hi', KEY)).rejects.toThrow('no message_id');
  });

  it('refuses a success body that is not the platform envelope', async () => {
    fetchMock.mockResolvedValue(new Response('not json', { status: 200 }));

    await expect(sendMessage('site-alpha', 'hi', KEY)).rejects.toThrow('not JSON');
  });
});

describe('answerQuestion', () => {
  it('posts the answer to the interaction, escaping the id in the path', async () => {
    fetchMock.mockResolvedValue(reply(200, { data: { status: 'answered' } }));

    await answerQuestion('a/b', true);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/channels/web/questions/a%2Fb/answer');
    expect(init.body).toBe(JSON.stringify({ answer: true }));
  });

  it('reports a question answered elsewhere in plain language', async () => {
    fetchMock.mockResolvedValue(reply(409, { error: 'question already answered elsewhere' }));

    await expect(answerQuestion('int-1', true)).rejects.toThrow(/already answered/i);
  });

  it('names the answer, not a message, when the door refuses the value', async () => {
    fetchMock.mockResolvedValue(reply(422, { error: 'answer must be a finite number' }));

    await expect(answerQuestion('int-1', 1e999)).rejects.toThrow("That answer couldn't be sent.");
  });
});

describe('rotateSession', () => {
  it('posts the web route the fresh session is minted for', async () => {
    // A session belongs to one route, and this door mints without reading a URL —
    // so the route it serves has to be in the body.
    fetchMock.mockResolvedValue(reply(200, { data: { status: 'rotated' } }));

    await rotateSession('site-alpha');

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/channels/web/session/rotate');
    expect(init.body).toBe(JSON.stringify({ identity: 'site-alpha' }));
  });

  it('surfaces a refusal', async () => {
    fetchMock.mockResolvedValue(reply(403, { error: 'cross-origin request refused' }));

    // The only 403 the doors this page FETCHES emit is an origin mismatch (the
    // mint guard's `not_a_navigation` answers the chat-page navigation, never a
    // fetch from this bundle). A reload cannot change the origin, so the copy must
    // not send the visitor after that fix.
    const error = await rotateSession('site-alpha').catch((err: unknown) => err);

    expect((error as ChatApiError).message).toBe('This chat only works on the site it belongs to.');
    expect((error as ChatApiError).message).not.toMatch(/reload|stale/i);
  });
});

describe('openChatStream', () => {
  it('gives every open a distinct URL', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 200 }));
    const signal = new AbortController().signal;

    await openChatStream('site-alpha', signal);
    await openChatStream('site-alpha', signal);

    const first = fetchMock.mock.calls[0]?.[0] as string;
    const second = fetchMock.mock.calls[1]?.[0] as string;
    expect(first).toContain('/api/channels/web/stream?identity=site-alpha&_=');
    expect(first).not.toBe(second);
  });

  it('flags an unconfigured transcript store as terminal', async () => {
    fetchMock.mockResolvedValue(
      reply(501, { error: 'store off', code: 'web_transcript_store_off' }),
    );

    const error = await openChatStream('site-alpha', new AbortController().signal).catch(
      (err: unknown) => err,
    );

    expect(isStoreOff(error)).toBe(true);
  });

  it('falls back to generic copy when the failure body is not an envelope', async () => {
    fetchMock.mockResolvedValue(new Response('<html>gateway</html>', { status: 502 }));

    const error = await openChatStream('site-alpha', new AbortController().signal).catch(
      (err: unknown) => err,
    );

    expect((error as ChatApiError).message).toMatch(/something went wrong/i);
    expect((error as ChatApiError).code).toBeNull();
  });
});
