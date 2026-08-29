import { StrictMode, type ReactElement } from 'react';
import {
  act,
  cleanup,
  createEvent,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatApiError } from '@/api';
import { ChatApp } from '@/app';
import type { ChatItem, ChatStreamState } from '@/use-chat-stream';

const api = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  answerQuestion: vi.fn(),
  rotateSession: vi.fn(),
}));

vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...api,
}));

// The stream is driven by hand: the page's own behaviour (optimistic sends, the
// typing bubble, the terminal states) is what these tests are about, and the fold
// itself is covered against the wire in `use-chat-stream.test.ts`.
const stream = vi.hoisted(() => ({ state: null as unknown }));

vi.mock('@/use-chat-stream', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/use-chat-stream')>()),
  useChatStream: () => stream.state,
}));

const TS = new Date().toISOString();

/** The idempotency key shape the door's contract accepts. */
const CLIENT_MESSAGE_ID = /^[A-Za-z0-9_-]{8,64}$/;

function streamState(overrides: Partial<ChatStreamState> = {}): ChatStreamState {
  return {
    items: [],
    answeredIds: new Set(),
    connected: true,
    backlogLoaded: true,
    error: null,
    disabled: false,
    sessionExpired: false,
    ...overrides,
  };
}

function agentSaid(id: string, text: string): ChatItem {
  return { kind: 'message', id, direction: 'out', text, ts: TS, clientMessageId: null };
}

/** The visitor's own message as the transcript replays it. `clientMessageId` is the
 * idempotency key the door echoes back onto the sender's own frame; `null` stands
 * for a message sent without one, which only the entry id can match. */
function visitorSaid(id: string, text: string, clientMessageId: string | null = null): ChatItem {
  return { kind: 'message', id, direction: 'in', text, ts: TS, clientMessageId };
}

function agentSentMedia(id: string, options: readonly string[]): ChatItem {
  return {
    kind: 'media',
    id,
    text: 'Here you go',
    media: [{ kind: 'image', url: 'https://example.com/a.png', caption: 'Item A' }],
    options,
    ts: TS,
  };
}

function pendingQuestion(): ChatItem {
  return {
    kind: 'question',
    id: 'q1',
    interactionId: 'int-1',
    question: 'Deploy?',
    answerFormat: 'confirm',
    options: null,
    media: null,
    callbackUrl: null,
    schema: null,
    timeoutAt: new Date(Date.now() + 600_000).toISOString(),
    ts: TS,
  };
}

function app(): ReactElement {
  return <ChatApp identity="site-alpha" title="Chat" />;
}

beforeEach(() => {
  stream.state = streamState();
  api.sendMessage.mockResolvedValue('msg-1');
  api.answerQuestion.mockResolvedValue(undefined);
  api.rotateSession.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function send(user: ReturnType<typeof userEvent.setup>, text: string): Promise<void> {
  await user.type(screen.getByLabelText('Message'), text);
  await user.click(screen.getByRole('button', { name: 'Send message' }));
}

describe('sending', () => {
  it('shows the message at once, then marks it sent when the door accepts it', async () => {
    const user = userEvent.setup();
    let accept = (_id: string): void => {};
    api.sendMessage.mockReturnValue(
      new Promise<string>((resolve) => {
        accept = resolve;
      }),
    );
    render(app());

    await send(user, 'hello there');

    expect(screen.getByText('hello there')).toBeInTheDocument();
    expect(screen.getByLabelText('Sending')).toBeInTheDocument();

    accept('msg-1');
    expect(await screen.findByLabelText('Sent')).toBeInTheDocument();
    expect(api.sendMessage).toHaveBeenCalledWith(
      'site-alpha',
      'hello there',
      expect.stringMatching(CLIENT_MESSAGE_ID),
    );
  });

  it('clears the composer and puts focus back for the next message', async () => {
    const user = userEvent.setup();
    render(app());

    await send(user, 'hello');
    expect(await screen.findByLabelText('Sent')).toBeInTheDocument();

    const field = screen.getByLabelText('Message');
    expect(field).toHaveValue('');
    expect(field).toHaveFocus();
  });

  it('sends on Enter and keeps Shift+Enter for a new line', async () => {
    const user = userEvent.setup();
    render(app());
    const field = screen.getByLabelText('Message');

    await user.type(field, 'line one{Shift>}{Enter}{/Shift}line two');
    expect(field).toHaveValue('line one\nline two');
    expect(api.sendMessage).not.toHaveBeenCalled();

    await user.type(field, '{Enter}');
    expect(await screen.findByLabelText('Sent')).toBeInTheDocument();
    expect(api.sendMessage).toHaveBeenCalledWith(
      'site-alpha',
      'line one\nline two',
      expect.stringMatching(CLIENT_MESSAGE_ID),
    );
  });

  it('leaves Enter to the IME while a candidate is being composed', async () => {
    const user = userEvent.setup();
    render(app());
    const field = screen.getByLabelText('Message');

    await user.type(field, 'にほんご');

    // The Enter that CONFIRMS an IME candidate — it ends the composition, not the
    // message. Sending here would deliver a half-composed line, and taking the
    // key's default would destroy the composition itself: it has to reach the IME
    // untouched, so "not sent" is only half of what this key must do.
    const composing = createEvent.keyDown(field, { key: 'Enter', isComposing: true });
    fireEvent(field, composing);
    expect(api.sendMessage).not.toHaveBeenCalled();
    expect(composing.defaultPrevented).toBe(false);

    // Safari reports the same key as the legacy 229 code rather than as composing.
    const legacy = createEvent.keyDown(field, { key: 'Enter', keyCode: 229 });
    fireEvent(field, legacy);
    expect(api.sendMessage).not.toHaveBeenCalled();
    expect(legacy.defaultPrevented).toBe(false);

    // The Enter after the composition is the visitor's own: it sends, and its
    // default — a newline in the field — is taken.
    const sends = createEvent.keyDown(field, { key: 'Enter' });
    fireEvent(field, sends);
    expect(sends.defaultPrevented).toBe(true);
    await waitFor(() =>
      expect(api.sendMessage).toHaveBeenCalledWith(
        'site-alpha',
        'にほんご',
        expect.stringMatching(CLIENT_MESSAGE_ID),
      ),
    );
    expect(api.sendMessage).toHaveBeenCalledTimes(1);
  });

  it('retires the twin off its own key when the response is lost, with no Retry pressed', async () => {
    const user = userEvent.setup();
    // The attempt reached the bridge and was delivered; only the response was lost,
    // so this page never learns the id of the entry its own send created.
    api.sendMessage.mockRejectedValueOnce(new ChatApiError('That did not get through.', 500, null));
    const { rerender } = render(app());

    await send(user, 'hello');
    expect(await screen.findByRole('alert')).toHaveTextContent('That did not get through');
    const key = (api.sendMessage.mock.calls[0] as [string, string, string])[2];
    expect(key).toMatch(CLIENT_MESSAGE_ID);

    // The delivery that lost response belonged to is on the transcript already, and
    // the door echoed this send's own key back onto it.
    stream.state = streamState({ items: [visitorSaid('msg-1', 'hello', key)] });
    rerender(app());

    // With no id to match on, the visitor would otherwise be left reading their own
    // message twice — one of the two wearing a failure — until they pressed a Retry
    // that could only re-send what had already arrived.
    await waitFor(() => expect(screen.getAllByText('hello')).toHaveLength(1));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(api.sendMessage).toHaveBeenCalledTimes(1);
  });

  it('keeps a refused message on screen with its reason and a retry', async () => {
    const user = userEvent.setup();
    api.sendMessage.mockRejectedValueOnce(
      new ChatApiError("I'm getting a lot of messages right now.", 503, null),
    );
    render(app());

    await send(user, 'hello');

    expect(await screen.findByRole('alert')).toHaveTextContent('a lot of messages');

    api.sendMessage.mockResolvedValueOnce('msg-9');
    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByLabelText('Sent')).toBeInTheDocument();
    expect(api.sendMessage).toHaveBeenCalledTimes(2);
    // The retry carries the SAME idempotency key as the attempt it repeats, so an
    // attempt the door had in fact accepted can never be delivered twice.
    const first = api.sendMessage.mock.calls[0] as [string, string, string];
    const second = api.sendMessage.mock.calls[1] as [string, string, string];
    expect(first[2]).toMatch(CLIENT_MESSAGE_ID);
    expect(second[2]).toBe(first[2]);
  });

  it('retires the optimistic bubble once the real transcript entry arrives', async () => {
    const user = userEvent.setup();
    const { rerender } = render(app());

    await send(user, 'hello');
    expect(await screen.findByLabelText('Sent')).toBeInTheDocument();

    // A frame carrying no key of its own: the entry id is the whole match here.
    stream.state = streamState({ items: [visitorSaid('msg-1', 'hello')] });
    rerender(app());

    await waitFor(() => expect(screen.queryByLabelText('Sent')).not.toBeInTheDocument());
    expect(screen.getByText('hello')).toBeInTheDocument();
  });

  it('retires it just the same when the echo beats the door response', async () => {
    const user = userEvent.setup();
    let accept = (_id: string): void => {};
    api.sendMessage.mockReturnValue(
      new Promise<string>((resolve) => {
        accept = resolve;
      }),
    );
    const { rerender } = render(app());

    await send(user, 'hello');

    // The door writes the transcript entry BEFORE it answers the POST, so the
    // stream's echo routinely lands first — while the page still has no id to
    // match it against.
    stream.state = streamState({ items: [visitorSaid('msg-1', 'hello')] });
    rerender(app());
    expect(screen.getAllByText('hello')).toHaveLength(2);

    await act(async () => {
      accept('msg-1');
    });

    expect(screen.getAllByText('hello')).toHaveLength(1);
    expect(screen.queryByLabelText('Sent')).not.toBeInTheDocument();
  });
});

describe('invite pairing', () => {
  // Each test sets the page URL by hand; reset it so a leftover `pair` cannot leak
  // into the next render (the pairing effect runs on every mount).
  afterEach(() => {
    window.history.replaceState({}, '', '/');
  });

  it('submits a valid pair code once as the first message, then strips it from the URL', async () => {
    window.history.replaceState({}, '', '/api/channels/web/chat/site-alpha?tai_pair=LINK-ABCD1234');
    render(app());

    await waitFor(() =>
      expect(api.sendMessage).toHaveBeenCalledWith(
        'site-alpha',
        'LINK-ABCD1234',
        expect.stringMatching(CLIENT_MESSAGE_ID),
      ),
    );
    expect(api.sendMessage).toHaveBeenCalledTimes(1);
    // The code rode the normal send path, so it shows as the visitor's own first bubble.
    expect(screen.getByText('LINK-ABCD1234')).toBeInTheDocument();
    // Stripped from the URL so a reload or a shared link cannot resubmit it.
    expect(window.location.search).toBe('');
  });

  it('removes only the pair parameter, leaving the rest of the URL intact', async () => {
    window.history.replaceState(
      {},
      '',
      '/api/channels/web/chat/site-alpha?ref=email&tai_pair=LINK-ABCD1234&x=1',
    );
    render(app());

    await waitFor(() => expect(api.sendMessage).toHaveBeenCalledTimes(1));
    const params = new URLSearchParams(window.location.search);
    expect(params.get('tai_pair')).toBeNull();
    expect(params.get('ref')).toBe('email');
    expect(params.get('x')).toBe('1');
  });

  it('submits the pair code only once, even as the page re-renders', async () => {
    window.history.replaceState({}, '', '/api/channels/web/chat/site-alpha?tai_pair=LINK-ABCD1234');
    const { rerender } = render(app());

    await waitFor(() => expect(api.sendMessage).toHaveBeenCalledTimes(1));

    // A stream frame re-renders the page; the code must not be resubmitted.
    stream.state = streamState({ items: [agentSaid('m1', 'hi')] });
    rerender(app());
    expect(api.sendMessage).toHaveBeenCalledTimes(1);
  });

  it('submits the pair code exactly once under a StrictMode double-invoke', async () => {
    window.history.replaceState({}, '', '/api/channels/web/chat/site-alpha?tai_pair=LINK-ABCD1234');
    render(<StrictMode>{app()}</StrictMode>);

    // StrictMode mounts, tears the effect down, then remounts and re-runs it — a replay
    // that exposes any mount-time work not guarded against running twice. The pairing
    // ref-guard rides through it: the code is submitted once, not once per mount.
    await waitFor(() => expect(api.sendMessage).toHaveBeenCalledTimes(1));
    expect(api.sendMessage).toHaveBeenCalledWith(
      'site-alpha',
      'LINK-ABCD1234',
      expect.stringMatching(CLIENT_MESSAGE_ID),
    );
    expect(window.location.search).toBe('');
  });

  it('does nothing when there is no pair parameter', () => {
    window.history.replaceState({}, '', '/api/channels/web/chat/site-alpha');
    render(app());

    expect(api.sendMessage).not.toHaveBeenCalled();
  });

  it.each([
    'link-abcd1234', // lower-case
    'LINK-abcd1234', // lower-case body
    'LINK-ABCD123', // too short
    'LINK-ABCD12345', // too long
    'xLINK-ABCD1234', // leading noise
    'LINK-ABCD1234x', // trailing noise
    'LINK-ABCD 234', // a non-alphanumeric in the body
    'LINK-ABCD1234<script>alert(1)</script>', // script tag trailing an otherwise-valid code
    '"><img src=x onerror=alert(1)>', // attribute-breakout markup
  ])('ignores a pair value that does not match the code shape: %s', (bad) => {
    window.history.replaceState(
      {},
      '',
      `/api/channels/web/chat/site-alpha?tai_pair=${encodeURIComponent(bad)}`,
    );
    render(app());

    // Never submitted, never stripped, never reflected into the page.
    expect(api.sendMessage).not.toHaveBeenCalled();
    expect(new URLSearchParams(window.location.search).get('tai_pair')).toBe(bad);
    expect(screen.queryByText(bad)).not.toBeInTheDocument();
  });
});

describe('typing indicator', () => {
  it('runs from an accepted send until the agent answers', async () => {
    const user = userEvent.setup();
    const { rerender } = render(app());

    expect(screen.queryByLabelText('Typing a reply')).not.toBeInTheDocument();

    await send(user, 'hello');
    expect(await screen.findByLabelText('Typing a reply')).toBeInTheDocument();

    stream.state = streamState({ items: [agentSaid('m2', 'Hi!')] });
    rerender(app());

    await waitFor(() => expect(screen.queryByLabelText('Typing a reply')).not.toBeInTheDocument());
  });

  it('clears when the agent turn is a media card', async () => {
    const user = userEvent.setup();
    const { rerender } = render(app());

    await send(user, 'hello');
    expect(await screen.findByLabelText('Typing a reply')).toBeInTheDocument();

    stream.state = streamState({ items: [agentSentMedia('md1', ['See all'])] });
    rerender(app());

    await waitFor(() => expect(screen.queryByLabelText('Typing a reply')).not.toBeInTheDocument());
  });

  it('never starts when the send was refused', async () => {
    const user = userEvent.setup();
    api.sendMessage.mockRejectedValueOnce(new ChatApiError('nope', 500, null));
    render(app());

    await send(user, 'hello');

    await screen.findByRole('alert');
    expect(screen.queryByLabelText('Typing a reply')).not.toBeInTheDocument();
  });

  it('stops when the stream drops — the reply cannot arrive on a dead stream', async () => {
    const user = userEvent.setup();
    const { rerender } = render(app());

    await send(user, 'hello');
    expect(await screen.findByLabelText('Typing a reply')).toBeInTheDocument();

    stream.state = streamState({ connected: false, error: new Error('dropped') });
    rerender(app());

    await waitFor(() => expect(screen.queryByLabelText('Typing a reply')).not.toBeInTheDocument());
    expect(screen.getByText('Reconnecting…')).toBeInTheDocument();
  });

  it('re-arms for a second send whose reply is still owed', async () => {
    const user = userEvent.setup();
    const { rerender } = render(app());

    await send(user, 'one');
    expect(await screen.findByLabelText('Typing a reply')).toBeInTheDocument();

    let accept = (_id: string): void => {};
    api.sendMessage.mockReturnValue(
      new Promise<string>((resolve) => {
        accept = resolve;
      }),
    );
    await send(user, 'two');

    // The reply to the FIRST send lands while the second is still in flight: it
    // settles the first wait and must not settle the second.
    stream.state = streamState({ items: [agentSaid('m2', 'Hi!')] });
    rerender(app());
    await waitFor(() => expect(screen.queryByLabelText('Typing a reply')).not.toBeInTheDocument());

    await act(async () => {
      accept('msg-2');
    });

    expect(screen.getByLabelText('Typing a reply')).toBeInTheDocument();
  });
});

describe('new conversation', () => {
  it('confirms first, then rotates the session and clears the screen', async () => {
    const user = userEvent.setup();
    render(app());

    await send(user, 'hello');
    expect(await screen.findByLabelText('Sent')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'New conversation' }));
    expect(api.rotateSession).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Start new' }));

    await waitFor(() => expect(api.rotateSession).toHaveBeenCalledTimes(1));
    // The fresh session is minted for THIS page's web route — a session serves one.
    // No `?tai_entry=` on this page URL, so the rotate carries no entry code.
    expect(api.rotateSession).toHaveBeenCalledWith('site-alpha', null);
    expect(await screen.findByText('Start the conversation')).toBeInTheDocument();
    expect(screen.queryByText('hello')).not.toBeInTheDocument();
  });

  it('re-presents the URL entry code on a rotation and never strips it', async () => {
    window.history.replaceState({}, '', '/api/channels/web/chat/site-alpha?tai_entry=code-xyz');
    try {
      const user = userEvent.setup();
      render(app());

      await user.click(screen.getByRole('button', { name: 'New conversation' }));
      await user.click(screen.getByRole('button', { name: 'Start new' }));

      await waitFor(() => expect(api.rotateSession).toHaveBeenCalledTimes(1));
      // A gated route admits the fresh session only with the code from the URL.
      expect(api.rotateSession).toHaveBeenCalledWith('site-alpha', 'code-xyz');
      // Unlike `pair`, the entry code is NOT stripped — a reload must re-present it.
      expect(new URLSearchParams(window.location.search).get('tai_entry')).toBe('code-xyz');
    } finally {
      window.history.replaceState({}, '', '/');
    }
  });

  it('keeps a send from the old conversation out of the fresh one', async () => {
    const user = userEvent.setup();
    let accept = (_id: string): void => {};
    api.sendMessage.mockReturnValue(
      new Promise<string>((resolve) => {
        accept = resolve;
      }),
    );
    render(app());

    await send(user, 'hello');
    await user.click(screen.getByRole('button', { name: 'New conversation' }));
    await user.click(screen.getByRole('button', { name: 'Start new' }));
    expect(await screen.findByText('Start the conversation')).toBeInTheDocument();

    // The old send lands after the rotate. Nobody owes the new conversation a
    // reply, so it must not open with a typing bubble that never clears.
    await act(async () => {
      accept('msg-1');
    });

    expect(screen.queryByLabelText('Typing a reply')).not.toBeInTheDocument();
    expect(screen.getByText('Start the conversation')).toBeInTheDocument();
  });

  it('keeps a refusal from the old conversation from killing the fresh one', async () => {
    const user = userEvent.setup();
    let refuse = (_err: unknown): void => {};
    api.sendMessage.mockReturnValue(
      new Promise<string>((_resolve, reject) => {
        refuse = reject;
      }),
    );
    render(app());

    await send(user, 'hello');
    await user.click(screen.getByRole('button', { name: 'New conversation' }));
    await user.click(screen.getByRole('button', { name: 'Start new' }));
    expect(await screen.findByText('Start the conversation')).toBeInTheDocument();

    // The session that refused it is the one the visitor just left — the session
    // they are on now was minted by the rotate and is alive.
    await act(async () => {
      refuse(new ChatApiError('Your chat session ended.', 401, 'session_missing'));
    });

    expect(screen.queryByText(/This conversation has ended/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('Message')).toBeEnabled();
    expect(screen.getByRole('button', { name: 'New conversation' })).toBeEnabled();
  });

  it('keeps an answer from the old conversation from killing the fresh one', async () => {
    const user = userEvent.setup();
    let refuse = (_err: unknown): void => {};
    api.answerQuestion.mockReturnValue(
      new Promise<void>((_resolve, reject) => {
        refuse = reject;
      }),
    );
    stream.state = streamState({ items: [pendingQuestion()] });
    const { rerender } = render(app());

    await user.click(screen.getByRole('button', { name: 'Yes' }));

    // The rotate drops the session the answer was sent on, and the fresh stream
    // carries none of the old conversation's items.
    await user.click(screen.getByRole('button', { name: 'New conversation' }));
    await user.click(screen.getByRole('button', { name: 'Start new' }));
    stream.state = streamState();
    rerender(app());
    expect(await screen.findByText('Start the conversation')).toBeInTheDocument();

    // The session that refused the answer is the one the visitor just left — the
    // session they are on now was minted by the rotate and is alive.
    await act(async () => {
      refuse(new ChatApiError('Your chat session ended.', 401, 'session_missing'));
    });

    expect(screen.queryByText(/This conversation has ended/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('Message')).toBeEnabled();
    expect(screen.getByRole('button', { name: 'New conversation' })).toBeEnabled();
  });

  it('opens the fresh conversation at the tail, however the old one was left', async () => {
    const user = userEvent.setup();
    stream.state = streamState({ items: [agentSaid('m1', 'one'), agentSaid('m2', 'two')] });
    const { rerender } = render(app());

    // jsdom measures nothing, so the region's geometry is declared outright: the
    // visitor has scrolled up, which detaches the follow and reveals the jump.
    const region = screen.getByTestId('transcript');
    Object.defineProperty(region, 'scrollHeight', { value: 1000, configurable: true });
    Object.defineProperty(region, 'clientHeight', { value: 100, configurable: true });
    region.scrollTop = 0;
    fireEvent.scroll(region);
    expect(screen.getByTestId('jump-to-latest')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'New conversation' }));
    await user.click(screen.getByRole('button', { name: 'Start new' }));
    stream.state = streamState();
    rerender(app());
    expect(await screen.findByText('Start the conversation')).toBeInTheDocument();

    // Where the visitor was reading belongs to the conversation they left. An
    // empty fresh one has nothing below the fold and nothing missed, so offering
    // to jump to a latest that is the only thing on screen is a leftover.
    expect(screen.queryByTestId('jump-to-latest')).not.toBeInTheDocument();
  });

  it('leaves the conversation alone when the rotate is refused', async () => {
    const user = userEvent.setup();
    api.rotateSession.mockRejectedValueOnce(new ChatApiError('This page went stale.', 403, null));
    render(app());

    await user.click(screen.getByRole('button', { name: 'New conversation' }));
    await user.click(screen.getByRole('button', { name: 'Start new' }));

    expect(await screen.findByText('This page went stale.')).toBeInTheDocument();
  });
});

describe('terminal states', () => {
  it('asks for a reload once the session is gone, and refuses to send', () => {
    stream.state = streamState({ sessionExpired: true });
    render(app());

    expect(screen.getByRole('alert')).toHaveTextContent('Reload the page');
    expect(screen.getByLabelText('Message')).toBeDisabled();
  });

  it('ends the conversation when a send reports the session is gone', async () => {
    const user = userEvent.setup();
    api.sendMessage.mockRejectedValueOnce(
      new ChatApiError('Your chat session ended.', 401, 'session_missing'),
    );
    render(app());

    await send(user, 'hello');

    expect(await screen.findByText(/Reload the page to start a new one/)).toBeInTheDocument();
  });

  it('says so plainly when the deployment runs no transcript store', () => {
    stream.state = streamState({ disabled: true, backlogLoaded: false });
    render(app());

    expect(screen.getByRole('alert')).toHaveTextContent('not switched on');
  });

  it('offers a retry when the backlog never arrived', async () => {
    const user = userEvent.setup();
    stream.state = streamState({
      backlogLoaded: false,
      connected: false,
      error: new Error('down'),
    });
    render(app());

    expect(screen.getByRole('alert')).toHaveTextContent("can't reach the conversation");
    await user.click(screen.getByRole('button', { name: 'Retry' }));
  });

  it('shows an unobtrusive pill while a loaded conversation reconnects', () => {
    stream.state = streamState({ connected: false, error: new Error('dropped') });
    render(app());

    expect(screen.getByText('Reconnecting…')).toBeInTheDocument();
    expect(screen.getByLabelText('Message')).toBeEnabled();
    // The pill is the ONE surface that announces a dropped connection. The
    // header's light spells its own state out as a label beside it, so a
    // connection that flaps is not read out twice on every flap.
    expect(screen.getByText('Not connected').closest('[role="status"]')).toBeNull();
  });

  it('says so when a frame on a live conversation could not be read', () => {
    stream.state = streamState({ error: new Error('malformed transcript frame: chat.question') });
    render(app());

    expect(screen.getByText(/couldn't be shown/)).toBeInTheDocument();
    expect(screen.queryByText('Reconnecting…')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Message')).toBeEnabled();
  });
});

describe('questions', () => {
  it('answers through the door and returns focus to the composer', async () => {
    const user = userEvent.setup();
    stream.state = streamState({ items: [pendingQuestion()] });
    render(app());

    await user.click(screen.getByRole('button', { name: 'Yes' }));

    expect(api.answerQuestion).toHaveBeenCalledWith('int-1', true);
    expect(screen.getByLabelText('Message')).toHaveFocus();
  });

  it('ends the conversation when an answer reports the session is gone', async () => {
    const user = userEvent.setup();
    api.answerQuestion.mockRejectedValueOnce(
      new ChatApiError('Your chat session ended.', 401, 'session_missing'),
    );
    stream.state = streamState({ items: [pendingQuestion()] });
    render(app());

    await user.click(screen.getByRole('button', { name: 'Yes' }));

    // The cookie is the whole credential: a question the door no longer knows the
    // session for takes the page down with it, rather than leaving live widgets
    // and a composer on a session that can accept nothing.
    expect(screen.getByText(/Reload the page to start a new one/)).toBeInTheDocument();
    expect(screen.getByText('Session ended')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Yes' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Message')).toBeDisabled();
  });
});

describe('media cards', () => {
  it('renders an agent media card and feeds a chip tap back as a visitor message', async () => {
    const user = userEvent.setup();
    stream.state = streamState({ items: [agentSentMedia('md1', ['See all'])] });
    render(app());

    expect(screen.getByRole('img', { name: 'Item A' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'See all' }));

    // The chip rides the composer's own send door: an optimistic bubble appears
    // beside the still-tappable chip, and the message goes out exactly as a typed
    // one would.
    expect(screen.getAllByText('See all')).toHaveLength(2);
    await waitFor(() =>
      expect(api.sendMessage).toHaveBeenCalledWith(
        'site-alpha',
        'See all',
        expect.stringMatching(CLIENT_MESSAGE_ID),
      ),
    );
  });

  it('sends the chip without disturbing a typed-but-unsent draft', async () => {
    const user = userEvent.setup();
    stream.state = streamState({ items: [agentSentMedia('md1', ['See all'])] });
    render(app());

    const field = screen.getByLabelText('Message');
    await user.type(field, 'half a thought');
    expect(field).toHaveValue('half a thought');

    await user.click(screen.getByRole('button', { name: 'See all' }));

    // The chip's own text goes out, and the visitor's unsent draft is left in the
    // composer untouched — only the composer's own submission clears it.
    await waitFor(() =>
      expect(api.sendMessage).toHaveBeenCalledWith(
        'site-alpha',
        'See all',
        expect.stringMatching(CLIENT_MESSAGE_ID),
      ),
    );
    expect(field).toHaveValue('half a thought');
  });
});

describe('the mobile keyboard', () => {
  it('sizes the page from the visual viewport, and follows it as it moves', () => {
    const listeners = new Map<string, () => void>();
    const viewport = {
      height: 500,
      offsetTop: 20,
      addEventListener: (name: string, fn: () => void) => listeners.set(name, fn),
      removeEventListener: (name: string) => listeners.delete(name),
    };
    vi.stubGlobal('visualViewport', viewport);
    render(app());
    const page = document.querySelector<HTMLElement>('.tcw-app');

    expect(page?.style.getPropertyValue('--tcw-vh')).toBe('500px');
    expect(page?.style.getPropertyValue('--tcw-vv-top')).toBe('20px');

    viewport.height = 280;
    act(() => listeners.get('resize')?.());
    expect(page?.style.getPropertyValue('--tcw-vh')).toBe('280px');

    vi.unstubAllGlobals();
  });
});

describe('the confirm dialog', () => {
  it('rotates nothing when the visitor backs out', async () => {
    const user = userEvent.setup();
    render(app());

    await user.click(screen.getByRole('button', { name: 'New conversation' }));
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(api.rotateSession).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});
