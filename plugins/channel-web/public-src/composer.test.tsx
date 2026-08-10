import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { MAX_MESSAGE_CHARS } from '@/api';
import { Composer, charactersLeftAnnouncement } from '@/composer';

afterEach(cleanup);

function renderComposer(value: string): void {
  render(
    <Composer
      value={value}
      onChange={vi.fn()}
      onSend={vi.fn()}
      disabled={false}
      placeholder="Write a message…"
      inputRef={() => {}}
    />,
  );
}

/** A draft with exactly `left` characters of room under the cap. */
function draftWithRoom(left: number): string {
  return 'x'.repeat(MAX_MESSAGE_CHARS - left);
}

describe('charactersLeftAnnouncement', () => {
  it('speaks in coarse steps, and says nothing until the cap is in reach', () => {
    expect(charactersLeftAnnouncement(201)).toBe('');
    expect(charactersLeftAnnouncement(200)).toBe('200 characters left');
    expect(charactersLeftAnnouncement(101)).toBe('200 characters left');
    expect(charactersLeftAnnouncement(100)).toBe('100 characters left');
    expect(charactersLeftAnnouncement(1)).toBe('100 characters left');
    expect(charactersLeftAnnouncement(0)).toBe('You have reached the message length limit');
  });

  it('changes twice on the way to the cap, not two hundred times', () => {
    let changes = 0;
    let previous = charactersLeftAnnouncement(200);
    for (let left = 199; left >= 0; left -= 1) {
      const spoken = charactersLeftAnnouncement(left);
      if (spoken !== previous) changes += 1;
      previous = spoken;
    }

    expect(changes).toBe(2);
  });
});

describe('the message length cap', () => {
  it('holds the field to the door own text cap', () => {
    renderComposer('');

    // The door refuses a longer text outright, so the field never lets one be
    // written: the visitor is stopped where they are typing, not after sending.
    expect(screen.getByLabelText('Message')).toHaveAttribute(
      'maxlength',
      String(MAX_MESSAGE_CHARS),
    );
  });

  it('says nothing about length while the cap is far off', () => {
    renderComposer(draftWithRoom(201));

    expect(screen.queryByText(/characters left/)).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('');
  });

  it('counts the room left once the cap comes into reach', () => {
    renderComposer(draftWithRoom(200));

    // Presentational — a screen reader that heard every keystroke would talk over
    // the visitor as they type, so the spoken form is coarse.
    expect(screen.getByText('200 characters left', { selector: '.tcw-count' })).toHaveAttribute(
      'aria-hidden',
      'true',
    );
    expect(screen.getByRole('status')).toHaveTextContent('200 characters left');
  });

  it('counts the last character in the singular', () => {
    renderComposer(draftWithRoom(1));

    expect(screen.getByText('1 character left')).toBeInTheDocument();
  });

  it('says plainly when the field will take no more', () => {
    renderComposer(draftWithRoom(0));

    const count = screen.getByText('0 characters left');
    expect(count).toHaveClass('tcw-count--full');
    expect(screen.getByRole('status')).toHaveTextContent(
      'You have reached the message length limit',
    );
  });
});
