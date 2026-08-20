import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { MediaCard, type MediaCardItem } from '@/media-card';
import type { MediaItem } from '@/use-chat-stream';

afterEach(() => {
  cleanup();
});

const TS = '2026-08-07T10:00:00+00:00';

function card(overrides: Partial<MediaCardItem> = {}): MediaCardItem {
  return {
    kind: 'media',
    id: 'md1',
    text: 'Here you go',
    media: null,
    options: null,
    ts: TS,
    ...overrides,
  };
}

function image(url: string, caption: string | null): MediaItem {
  return { kind: 'image', url, caption };
}

function link(url: string, caption: string | null): MediaItem {
  return { kind: 'link', url, caption };
}

function renderCard(item: MediaCardItem, onSend = vi.fn(), locked = false) {
  const view = render(<MediaCard item={item} onSend={onSend} locked={locked} />);
  return { ...view, onSend };
}

describe('MediaCard', () => {
  it('renders an image with its source and its caption as the alt text', () => {
    renderCard(card({ media: [image('https://example.com/a.png', 'Item A')] }));

    const img = screen.getByRole('img', { name: 'Item A' });
    expect(img).toHaveAttribute('src', 'https://example.com/a.png');
    expect(img).toHaveAttribute('loading', 'lazy');
  });

  it('shows the caption as visible text beneath the image', () => {
    renderCard(card({ media: [image('https://example.com/a.png', 'Item A')] }));

    expect(screen.getByText('Item A').tagName).toBe('FIGCAPTION');
  });

  it('gives an image with no caption an empty alt and no caption line', () => {
    const { container } = renderCard(card({ media: [image('https://example.com/a.png', null)] }));

    expect(container.querySelector('img')).toHaveAttribute('alt', '');
    expect(container.querySelector('figcaption')).toBeNull();
  });

  it('renders the card text through markdown, not as raw source', () => {
    const { container } = renderCard(card({ text: '**bold**' }));

    const strong = container.querySelector('strong');
    expect(strong).not.toBeNull();
    expect(strong).toHaveTextContent('bold');
    expect(container.textContent).not.toContain('**');
  });

  it('renders a markdown javascript: link as inert text, never a live anchor', () => {
    const { container } = renderCard(card({ text: '[x](javascript:alert(1))' }));

    // No anchor carries a javascript: destination — the SDK neutralises the link.
    const anchors = Array.from(container.querySelectorAll('a'));
    expect(
      anchors.some((anchor) =>
        (anchor.getAttribute('href') ?? '').toLowerCase().startsWith('javascript:'),
      ),
    ).toBe(false);
    // The link is gone entirely; its label survives as plain text in a span.
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getByText('x').tagName).toBe('SPAN');
  });

  it('renders a hostile caption as text, never as markup', () => {
    const hostile = '<img onerror=alert(1) src=x>';
    const { container } = renderCard(
      card({ media: [image('https://example.com/a.png', hostile)] }),
    );

    // The only <img> is the card's own; the caption's markup did not become DOM.
    expect(container.querySelectorAll('img')).toHaveLength(1);
    expect(screen.getByText(hostile).tagName).toBe('FIGCAPTION');
  });

  it('opens a link attachment in a new tab with a safe rel', () => {
    renderCard(card({ media: [link('https://example.com/a', 'Open A')] }));

    const anchor = screen.getByRole('link', { name: 'Open A' });
    expect(anchor).toHaveAttribute('href', 'https://example.com/a');
    expect(anchor).toHaveAttribute('target', '_blank');
    expect(anchor).toHaveAttribute('rel', 'noreferrer noopener');
  });

  it('labels a captionless link with its own url', () => {
    renderCard(card({ media: [link('https://example.com/a', null)] }));

    expect(screen.getByRole('link', { name: 'https://example.com/a' })).toBeInTheDocument();
  });

  it('sends a chip label as a regular visitor message when tapped', async () => {
    const user = userEvent.setup();
    const { onSend } = renderCard(card({ options: ['Item A', 'Item B'] }));

    await user.click(screen.getByRole('button', { name: 'Item A' }));

    expect(onSend).toHaveBeenCalledWith('Item A');
  });

  it('keeps every chip tappable after a tap — no claim or settle', async () => {
    const user = userEvent.setup();
    const { onSend } = renderCard(card({ options: ['Item A', 'Item B'] }));

    await user.click(screen.getByRole('button', { name: 'Item A' }));
    await user.click(screen.getByRole('button', { name: 'Item A' }));

    expect(screen.getByRole('button', { name: 'Item A' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Item B' })).toBeEnabled();
    expect(onSend).toHaveBeenCalledTimes(2);
  });

  it('renders duplicate option labels as distinct chips without a key collision', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      renderCard(card({ options: ['Pick', 'Pick'] }));

      expect(screen.getAllByRole('button', { name: 'Pick' })).toHaveLength(2);
      const warned = spy.mock.calls.some(
        (call) => typeof call[0] === 'string' && call[0].includes('two children with the same key'),
      );
      expect(warned).toBe(false);
    } finally {
      spy.mockRestore();
    }
  });

  it('disables every chip when the session is locked', () => {
    renderCard(card({ options: ['Item A', 'Item B'] }), vi.fn(), true);

    expect(screen.getByRole('button', { name: 'Item A' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Item B' })).toBeDisabled();
  });

  it('leaves the chips tappable, and sending, when the session is not locked', async () => {
    const user = userEvent.setup();
    const { onSend } = renderCard(card({ options: ['Item A'] }), vi.fn(), false);

    const chip = screen.getByRole('button', { name: 'Item A' });
    expect(chip).toBeEnabled();
    await user.click(chip);

    expect(onSend).toHaveBeenCalledWith('Item A');
  });
});
