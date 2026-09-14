import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatApiError } from '@/api';
import {
  agentSaid,
  agentSentMedia,
  app,
  pendingQuestion,
  send,
  streamState,
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
