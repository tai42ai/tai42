import { screen } from '@testing-library/react';
import type userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';

import { ChatApp } from '@/app';
import type { ChatItem } from '@/transcript-model';
import type { ChatStreamState } from '@/use-chat-stream';

export const TS = new Date().toISOString();

/** The idempotency key shape the door's contract accepts. */
export const CLIENT_MESSAGE_ID = /^[A-Za-z0-9_-]{8,64}$/;

export function streamState(overrides: Partial<ChatStreamState> = {}): ChatStreamState {
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

export function agentSaid(id: string, text: string): ChatItem {
  return { kind: 'message', id, direction: 'out', text, ts: TS, clientMessageId: null };
}

/** The visitor's own message as the transcript replays it. `clientMessageId` is the
 * idempotency key the door echoes back onto the sender's own frame; `null` stands
 * for a message sent without one, which only the entry id can match. */
export function visitorSaid(
  id: string,
  text: string,
  clientMessageId: string | null = null,
): ChatItem {
  return { kind: 'message', id, direction: 'in', text, ts: TS, clientMessageId };
}

export function agentSentMedia(id: string, options: readonly string[]): ChatItem {
  return {
    kind: 'media',
    id,
    text: 'Here you go',
    media: [{ kind: 'image', url: 'https://example.com/a.png', caption: 'Item A', filename: null }],
    options: options.map((text) => ({ kind: 'reply', text, description: null, id: null })),
    sections: null,
    header: null,
    footer: null,
    location: null,
    ts: TS,
  };
}

export function agentSentForm(id: string): ChatItem {
  return {
    kind: 'form',
    id,
    text: 'Fill this in',
    schema: { type: 'object', properties: { note: { type: 'string' } } },
    token: 'tok-1',
    media: null,
    location: null,
    formData: null,
    pages: null,
    ts: TS,
  };
}

export function pendingQuestion(): ChatItem {
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
    formData: null,
    pages: null,
    timeoutAt: new Date(Date.now() + 600_000).toISOString(),
    ts: TS,
  };
}

export function app(): ReactElement {
  return <ChatApp identity="site-alpha" title="Chat" />;
}

export async function send(user: ReturnType<typeof userEvent.setup>, text: string): Promise<void> {
  await user.type(screen.getByLabelText('Message'), text);
  await user.click(screen.getByRole('button', { name: 'Send message' }));
}
