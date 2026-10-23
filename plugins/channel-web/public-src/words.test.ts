import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BUILT_IN_TRANSPORT_WORDS, fillToken, transportWords } from '@/words';

function mountRoot(words: string | null): void {
  const root = document.createElement('div');
  root.id = 'root';
  if (words !== null) root.dataset.words = words;
  document.body.replaceChildren(root);
}

beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  document.body.innerHTML = '';
  vi.restoreAllMocks();
});

describe('transportWords', () => {
  it("draws the route's own sentences over the built-ins", () => {
    mountRoot(
      JSON.stringify({ busy: 'Please wait a moment.', not_sent: '{subject} was refused.' }),
    );

    const words = transportWords();

    expect(words.busy).toBe('Please wait a moment.');
    expect(words.not_sent).toBe('{subject} was refused.');
    expect(words.failed).toBe(BUILT_IN_TRANSPORT_WORDS.failed);
    expect(console.error).not.toHaveBeenCalled();
  });

  it('draws the built-in sentences when the page carries no words', () => {
    mountRoot(null);

    expect(transportWords()).toEqual(BUILT_IN_TRANSPORT_WORDS);
    expect(console.error).not.toHaveBeenCalled();
  });

  it('reads the attribute once per page', () => {
    mountRoot(JSON.stringify({ busy: 'Please wait a moment.' }));
    const first = transportWords();
    document.getElementById('root')!.dataset.words = JSON.stringify({ busy: 'Changed.' });

    expect(transportWords()).toBe(first);
    expect(transportWords().busy).toBe('Please wait a moment.');
  });

  it.each([
    ['not JSON', '{busy'],
    ['not an object', '["busy"]'],
    ['an unknown member', JSON.stringify({ busy: 'Wait.', shout: 'Hey' })],
    ['a blank member', JSON.stringify({ busy: '   ' })],
    ['a non-string member', JSON.stringify({ busy: 3 })],
  ])('refuses a malformed attribute (%s) loudly and draws the built-ins', (_label, raw) => {
    mountRoot(raw);

    expect(transportWords()).toEqual(BUILT_IN_TRANSPORT_WORDS);
    expect(console.error).toHaveBeenCalledTimes(1);
  });
});

describe('fillToken', () => {
  it('fills every token with a plain split/join, never a pattern replace', () => {
    expect(fillToken('{reason} / {reason}', 'reason', '$& and $1')).toBe('$& and $1 / $& and $1');
  });

  it('leaves a sentence without the token as it is', () => {
    expect(fillToken('Not accepted.', 'reason', 'too late')).toBe('Not accepted.');
  });
});
