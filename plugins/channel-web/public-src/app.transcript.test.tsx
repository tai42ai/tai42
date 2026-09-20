import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatApiError } from '@/api';
import { agentSentForm, app, pendingQuestion, send, streamState } from '@/app.test-support';

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

describe('form cards', () => {
  it('renders an ask-less form card and submits it through the form door', async () => {
    const user = userEvent.setup();
    api.submitForm.mockResolvedValueOnce(undefined);
    stream.state = streamState({ items: [agentSentForm('f1')] });
    render(app());

    await user.type(screen.getByRole('textbox', { name: /note/i }), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() => expect(api.submitForm).toHaveBeenCalledWith('tok-1', { note: 'ship it' }));
    expect(await screen.findByText('Sent')).toBeInTheDocument();
  });

  it('ends the conversation when a submission reports the session is gone', async () => {
    const user = userEvent.setup();
    api.submitForm.mockRejectedValueOnce(
      new ChatApiError('Your chat session ended.', 401, 'session_missing'),
    );
    stream.state = streamState({ items: [agentSentForm('f1')] });
    render(app());

    await user.type(screen.getByRole('textbox', { name: /note/i }), 'ship it');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText(/Reload the page to start a new one/)).toBeInTheDocument();
    // The locked page withdraws the form's controls behind the badge.
    expect(screen.getByText('Session ended')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Send' })).not.toBeInTheDocument();
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
