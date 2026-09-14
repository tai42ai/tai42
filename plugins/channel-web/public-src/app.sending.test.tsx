import { StrictMode } from 'react';
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
import type { ChatItem } from '@/transcript-model';
import {
  CLIENT_MESSAGE_ID,
  TS,
  agentSaid,
  agentSentMedia,
  app,
  send,
  streamState,
  visitorSaid,
} from '@/app.test-support';

const api = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  answerQuestion: vi.fn(),
  rotateSession: vi.fn(),
  submitForm: vi.fn(),
}));

vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  ...api,
}));

// The stream is driven by hand: the page's own behaviour (optimistic sends, the
// typing bubble, the terminal states) is what these tests are about, and the fold
// itself is covered against the wire in the frame-reducer tests.
const stream = vi.hoisted(() => ({ state: null as unknown }));

vi.mock('@/use-chat-stream', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/use-chat-stream')>()),
  useChatStream: () => stream.state,
}));

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
      null,
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
      null,
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
        null,
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
        null,
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
      null,
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
        null,
      ),
    );
  });

  it('threads a tapped reply option authored id to the send door', async () => {
    const user = userEvent.setup();
    const withId: ChatItem = {
      kind: 'media',
      id: 'md-id',
      text: 'Pick one',
      media: null,
      options: [{ kind: 'reply', text: 'Item A', description: null, id: 'opt-a' }],
      sections: null,
      header: null,
      footer: null,
      location: null,
      ts: TS,
    };
    stream.state = streamState({ items: [withId] });
    render(app());

    await user.click(screen.getByRole('button', { name: /Item A/ }));

    await waitFor(() =>
      expect(api.sendMessage).toHaveBeenCalledWith(
        'site-alpha',
        'Item A',
        expect.stringMatching(CLIENT_MESSAGE_ID),
        'opt-a',
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
        null,
      ),
    );
    expect(field).toHaveValue('half a thought');
  });
});
