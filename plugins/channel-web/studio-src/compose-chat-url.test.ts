import { describe, expect, it } from 'vitest';

import { composeChatUrl } from './compose-chat-url';

describe('composeChatUrl', () => {
  it('joins the same origin, the mount base, the encoded identity and code', () => {
    expect(composeChatUrl('', '/api/channels/web', 'a b', 'x&y')).toBe(
      `${window.location.origin}/api/channels/web/chat/a%20b?tai_entry=x%26y`,
    );
  });

  it('prefers a configured absolute origin and strips its trailing slash', () => {
    expect(composeChatUrl('https://api.example/', '/api/chat-alt', 'id', 'c')).toBe(
      'https://api.example/api/chat-alt/chat/id?tai_entry=c',
    );
  });
});
