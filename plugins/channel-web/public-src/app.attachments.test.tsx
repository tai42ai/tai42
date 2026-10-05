import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatApiError } from '@/api';
import { app, CLIENT_MESSAGE_ID, streamState } from '@/app.test-support';

const api = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  uploadAttachment: vi.fn(),
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

const MEDIA_ID = 'M'.repeat(43);

function uploaded(overrides: Record<string, unknown> = {}) {
  return {
    media_id: MEDIA_ID,
    kind: 'image',
    mime: 'image/png',
    size: 2048,
    filename: null,
    url: `/api/interactions/media/${MEDIA_ID}`,
    ...overrides,
  };
}

function image(name = 'photo.png'): File {
  return new File(['x'], name, { type: 'image/png' });
}

/** Hand files to the composer's hidden file input, exactly as the picker does. */
function pick(files: File[]): void {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files } });
}

beforeEach(() => {
  stream.state = streamState();
  api.sendMessage.mockResolvedValue('msg-1');
  api.uploadAttachment.mockResolvedValue(uploaded());
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('sending with an attachment', () => {
  it('uploads on add, then sends the ready id and shows it on the optimistic bubble', async () => {
    const user = userEvent.setup();
    render(app());

    pick([image()]);
    await waitFor(() =>
      expect(screen.getByTestId('attach-item')).toHaveAttribute('data-status', 'ready'),
    );
    // The tray thumbnail is the client data URL — the served url 404s pre-bind. That
    // data: URL lands from an async FileReader read that can resolve after the item
    // reaches "ready", so the thumbnail is polled for inside waitFor until it appears.
    await waitFor(() => {
      const thumb = document.querySelector('.tcw-attach-thumb') as HTMLImageElement | null;
      expect(thumb?.src).toMatch(/^data:/);
    });

    await user.type(screen.getByLabelText('Message'), 'hello');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(api.sendMessage).toHaveBeenCalledWith(
      'site-alpha',
      'hello',
      expect.stringMatching(CLIENT_MESSAGE_ID),
      null,
      [MEDIA_ID],
    );
    // The optimistic bubble carries the client preview image.
    const bubbleImage = document.querySelector('.tcw-media-image') as HTMLImageElement;
    expect(bubbleImage.src).toMatch(/^data:/);
    // The sent item left the tray.
    await waitFor(() => expect(screen.queryByTestId('attach-item')).not.toBeInTheDocument());
  });

  it('sends only the ready id and keeps a failed item in the tray with its retry', async () => {
    const user = userEvent.setup();
    api.uploadAttachment
      .mockResolvedValueOnce(uploaded({ media_id: 'A'.repeat(43) }))
      .mockRejectedValueOnce(new ChatApiError('no', 415, 'media_type_not_allowed'));
    render(app());

    pick([image('ok.png'), new File(['x'], 'bad.pdf', { type: 'application/pdf' })]);
    const items = await screen.findAllByTestId('attach-item');
    expect(items).toHaveLength(2);
    await waitFor(() => expect(items[0]).toHaveAttribute('data-status', 'ready'));
    await waitFor(() => expect(items[1]).toHaveAttribute('data-status', 'failed'));

    await user.type(screen.getByLabelText('Message'), 'here you go');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    // Only the ready id rides the wire; the failed upload was never part of the send.
    expect(api.sendMessage).toHaveBeenCalledWith(
      'site-alpha',
      'here you go',
      expect.stringMatching(CLIENT_MESSAGE_ID),
      null,
      ['A'.repeat(43)],
    );
    // The failed item stays in the tray, still wearing its retry control.
    const remaining = await screen.findAllByTestId('attach-item');
    expect(remaining).toHaveLength(1);
    expect(remaining[0]).toHaveAttribute('data-status', 'failed');
    expect(
      screen.getByRole('button', { name: 'Retry bad.pdf — That file type is not supported.' }),
    ).toBeInTheDocument();
  });

  it('rejects a send whose attachment can no longer be bound, with its reason and no Retry', async () => {
    const user = userEvent.setup();
    api.sendMessage.mockRejectedValueOnce(
      new ChatApiError(
        'That attachment is no longer available — attach it again to send it.',
        400,
        'media_unbindable',
      ),
    );
    render(app());

    pick([image()]);
    await waitFor(() =>
      expect(screen.getByTestId('attach-item')).toHaveAttribute('data-status', 'ready'),
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    // The reason shows on the bubble. A Retry would re-send the same dead id and be
    // refused again, so none is offered; the visitor attaches the file afresh instead.
    expect(await screen.findByText(/That attachment is no longer available/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('keeps an offline-queued item in the tray when a text-only message is sent', async () => {
    const user = userEvent.setup();
    stream.state = streamState({ connected: false });
    render(app());

    pick([image('queued.png')]);
    await waitFor(() =>
      expect(screen.getByTestId('attach-item')).toHaveAttribute('data-status', 'pending'),
    );
    expect(api.uploadAttachment).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText('Message'), 'text now');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    // The queued (not-yet-uploaded) attachment carries no id, so the send is text-only
    // and the item stays queued in the tray for its reconnect retry.
    expect(api.sendMessage).toHaveBeenCalledWith(
      'site-alpha',
      'text now',
      expect.stringMatching(CLIENT_MESSAGE_ID),
      null,
      [],
    );
    expect(screen.getByTestId('attach-item')).toHaveAttribute('data-status', 'pending');
  });
});

describe('the drag-and-drop zone', () => {
  function fileDrag() {
    return { dataTransfer: { types: ['Files'], files: [], dropEffect: '' } };
  }

  it('toggles the drop-active outline on drag enter and leave', () => {
    render(app());
    const zone = screen.getByTestId('drop-zone');
    expect(zone).not.toHaveClass('tcw-drop-active');

    fireEvent.dragEnter(zone, fileDrag());
    expect(zone).toHaveClass('tcw-drop-active');

    fireEvent.dragLeave(zone, fileDrag());
    expect(zone).not.toHaveClass('tcw-drop-active');
  });
});
