import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AttachTray } from '@/attach-tray';
import type { AttachItem } from '@/uploads';

afterEach(cleanup);

function item(overrides: Partial<AttachItem> = {}): AttachItem {
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

function renderTray(
  overrides: {
    items?: readonly AttachItem[];
    announcement?: string;
    onRemove?: (id: string) => void;
    onRetry?: (id: string) => void;
    disabled?: boolean;
  } = {},
): { onRemove: ReturnType<typeof vi.fn>; onRetry: ReturnType<typeof vi.fn> } {
  const onRemove = overrides.onRemove ? (overrides.onRemove as ReturnType<typeof vi.fn>) : vi.fn();
  const onRetry = overrides.onRetry ? (overrides.onRetry as ReturnType<typeof vi.fn>) : vi.fn();
  render(
    <AttachTray
      items={overrides.items ?? [item()]}
      announcement={overrides.announcement ?? ''}
      onRemove={onRemove}
      onRetry={onRetry}
      disabled={overrides.disabled ?? false}
    />,
  );
  return { onRemove, onRetry };
}

describe('an empty tray', () => {
  it('renders only the live region, keeping the last announcement audible', () => {
    renderTray({ items: [], announcement: 'photo.png ready' });

    expect(screen.queryByTestId('compose-tray')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('photo.png ready');
  });
});

describe('a ready attachment', () => {
  it('shows the image preview from its data URL', () => {
    renderTray();

    const thumb = document.querySelector('.tcw-attach-thumb') as HTMLImageElement;
    expect(thumb.src).toMatch(/^data:image\/png/);
  });

  it('shows a document by name and offers a remove control', async () => {
    const user = userEvent.setup();
    const { onRemove } = renderTray({
      items: [item({ kind: 'document', filename: 'report.pdf', previewUrl: null })],
    });

    expect(screen.getByText('report.pdf')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Remove report.pdf' }));
    expect(onRemove).toHaveBeenCalledWith('a1');
  });
});

describe('an uploading attachment', () => {
  it('shows progress and a cancel control', async () => {
    const user = userEvent.setup();
    const { onRemove } = renderTray({ items: [item({ status: 'uploading', mediaId: null })] });

    expect(screen.getByRole('progressbar')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Cancel upload' }));
    expect(onRemove).toHaveBeenCalledWith('a1');
  });
});

describe('a failed attachment', () => {
  it('names the reason in the retry control label as well as its text', async () => {
    const user = userEvent.setup();
    const { onRetry } = renderTray({
      items: [
        item({ status: 'failed', mediaId: null, reason: 'That file type is not supported.' }),
      ],
    });

    // The label carries the reason too — an aria-label would otherwise mask the
    // visible reason text from a screen reader once the live announcement is gone.
    const retry = screen.getByRole('button', {
      name: 'Retry photo.png — That file type is not supported.',
    });
    expect(retry).toHaveTextContent('That file type is not supported.');
    await user.click(retry);
    expect(onRetry).toHaveBeenCalledWith('a1');
  });

  it('carries the failed status onto the row for styling', () => {
    renderTray({ items: [item({ status: 'failed', mediaId: null, reason: 'x' })] });

    expect(screen.getByTestId('attach-item')).toHaveAttribute('data-status', 'failed');
  });
});

describe('an ended session', () => {
  it('stops the retry and remove controls with the rest of the composer', () => {
    renderTray({
      items: [item({ status: 'failed', reason: 'Upload failed — tap to retry.' })],
      disabled: true,
    });

    expect(screen.getByRole('button', { name: /^Retry / })).toBeDisabled();
    expect(screen.getByRole('button', { name: /^Remove / })).toBeDisabled();
  });
});

describe('keyboard', () => {
  it('removes the focused item on Escape', async () => {
    const user = userEvent.setup();
    const { onRemove } = renderTray();

    // Escape while a control inside the item is focused bubbles to the row handler.
    screen.getByRole('button', { name: 'Remove photo.png' }).focus();
    await user.keyboard('{Escape}');

    expect(onRemove).toHaveBeenCalledWith('a1');
  });
});

describe('announcements', () => {
  it('speaks the latest lifecycle change through the tray live region', () => {
    renderTray({ announcement: 'Uploading photo.png…' });

    expect(screen.getByRole('status')).toHaveTextContent('Uploading photo.png…');
  });
});
