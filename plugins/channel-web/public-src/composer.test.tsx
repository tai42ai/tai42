import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { MAX_MESSAGE_CHARS } from '@/api';
import { charactersLeftAnnouncement, Composer, type ComposerProps } from '@/composer';
import type { AttachItem } from '@/uploads';

afterEach(cleanup);

function attachment(overrides: Partial<AttachItem> = {}): AttachItem {
  return {
    id: 'a1',
    file: new File(['x'], 'photo.png', { type: 'image/png' }),
    filename: 'photo.png',
    size: 2048,
    kind: 'image',
    status: 'ready',
    previewUrl: 'data:image/png;base64,AAAA',
    reason: null,
    mediaId: 'M'.repeat(43),
    url: '/api/interactions/media/' + 'M'.repeat(43),
    ...overrides,
  };
}

function renderComposer(overrides: Partial<ComposerProps> = {}): {
  onAddFiles: ReturnType<typeof vi.fn>;
} {
  const onAddFiles = vi.fn();
  render(
    <Composer
      value=""
      onChange={vi.fn()}
      onSend={vi.fn()}
      disabled={false}
      placeholder="Write a message…"
      inputRef={() => {}}
      attachments={[]}
      onAddFiles={onAddFiles}
      onRemoveAttachment={vi.fn()}
      onRetryAttachment={vi.fn()}
      attachAnnouncement=""
      uploadsInFlight={false}
      attachAtCap={false}
      maxAttachments={10}
      onAttachBlocked={vi.fn()}
      {...overrides}
    />,
  );
  return { onAddFiles };
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
    renderComposer();

    // The door refuses a longer text outright, so the field never lets one be
    // written: the visitor is stopped where they are typing, not after sending.
    expect(screen.getByLabelText('Message')).toHaveAttribute(
      'maxlength',
      String(MAX_MESSAGE_CHARS),
    );
  });

  it('says nothing about length while the cap is far off', () => {
    renderComposer({ value: draftWithRoom(201) });

    // Neither the presentational count nor a spoken length line is rendered while
    // the cap is out of reach.
    expect(screen.queryByText(/characters left/)).not.toBeInTheDocument();
    expect(document.querySelector('.tcw-count')).toBeNull();
  });

  it('counts the room left once the cap comes into reach', () => {
    renderComposer({ value: draftWithRoom(200) });

    // Presentational — a screen reader that heard every keystroke would talk over
    // the visitor as they type, so the spoken form is coarse.
    expect(screen.getByText('200 characters left', { selector: '.tcw-count' })).toHaveAttribute(
      'aria-hidden',
      'true',
    );
    expect(
      screen.getByText('200 characters left', { selector: '[role="status"]' }),
    ).toBeInTheDocument();
  });

  it('counts the last character in the singular', () => {
    renderComposer({ value: draftWithRoom(1) });

    expect(screen.getByText('1 character left')).toBeInTheDocument();
  });

  it('says plainly when the field will take no more', () => {
    renderComposer({ value: draftWithRoom(0) });

    const count = screen.getByText('0 characters left');
    expect(count).toHaveClass('tcw-count--full');
    expect(
      screen.getByText('You have reached the message length limit', {
        selector: '[role="status"]',
      }),
    ).toBeInTheDocument();
  });
});

describe('attaching a file', () => {
  it('offers a labelled attach control in the composer', () => {
    renderComposer();

    const attach = screen.getByRole('button', { name: 'Attach a file' });
    expect(attach).toBeInTheDocument();
    expect(attach).not.toBeDisabled();
  });

  it('disables the attach control when the session has ended', () => {
    renderComposer({ disabled: true });

    expect(screen.getByRole('button', { name: 'Attach a file' })).toBeDisabled();
  });

  it('hands picked files to the tray', () => {
    const { onAddFiles } = renderComposer();
    const file = new File(['x'], 'doc.pdf', { type: 'application/pdf' });

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });

    expect(onAddFiles).toHaveBeenCalledWith([file]);
  });

  it('captures a pasted image', () => {
    const { onAddFiles } = renderComposer();
    const image = new File(['x'], 'clip.png', { type: 'image/png' });

    fireEvent.paste(screen.getByLabelText('Message'), {
      clipboardData: {
        items: [{ kind: 'file', type: 'image/png', getAsFile: () => image }],
      },
    });

    expect(onAddFiles).toHaveBeenCalledWith([image]);
  });

  it('stops the attach control at the per-message cap, naming the limit', async () => {
    const user = userEvent.setup();
    const onAttachBlocked = vi.fn();
    const { onAddFiles } = renderComposer({
      attachAtCap: true,
      maxAttachments: 3,
      onAttachBlocked,
    });

    const attach = screen.getByRole('button', { name: 'Attach a file' });
    expect(attach).toHaveAttribute('aria-disabled', 'true');
    expect(attach).toHaveAttribute('title', 'You can attach up to 3 files per message.');

    // At the cap the picker never opens — a doomed selection is refused before it is
    // made — and the reason is announced for whoever cannot see the tooltip.
    await user.click(attach);
    expect(onAddFiles).not.toHaveBeenCalled();
    expect(onAttachBlocked).toHaveBeenCalledTimes(1);
  });

  it('leaves the attach control open below the cap', () => {
    renderComposer({ attachAtCap: false, maxAttachments: 3 });

    const attach = screen.getByRole('button', { name: 'Attach a file' });
    expect(attach).not.toHaveAttribute('aria-disabled');
    expect(attach).not.toHaveAttribute('title');
  });
});

describe('when a send may go out', () => {
  it('keeps send off while an upload is still in flight', () => {
    renderComposer({
      value: 'ready to go',
      attachments: [attachment({ status: 'uploading' })],
      uploadsInFlight: true,
    });

    // A half-uploaded file cannot be referenced, so the whole send waits for it.
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
  });

  it('enables send on a ready attachment even with no typed text', () => {
    renderComposer({ value: '', attachments: [attachment()] });

    expect(screen.getByRole('button', { name: 'Send message' })).not.toBeDisabled();
  });

  it('keeps send off with neither text nor a ready attachment', () => {
    renderComposer({ value: '', attachments: [attachment({ status: 'failed', mediaId: null })] });

    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
  });

  it('sends a caption-less attachment on Enter', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({ value: '', attachments: [attachment()], onSend });

    await user.type(screen.getByLabelText('Message'), '{Enter}');

    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it('does not send on Enter while an upload is in flight', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderComposer({
      value: 'text',
      attachments: [attachment({ status: 'uploading' })],
      uploadsInFlight: true,
      onSend,
    });

    await user.type(screen.getByLabelText('Message'), '{Enter}');

    expect(onSend).not.toHaveBeenCalled();
  });
});
