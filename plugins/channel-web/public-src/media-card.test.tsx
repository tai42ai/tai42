import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { MediaCard, type MediaCardItem, type SendReply } from '@/media-card';
import type { CardOption, LocationPoint, MediaItem, OptionSection } from '@/use-chat-stream';

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
    sections: null,
    header: null,
    footer: null,
    location: null,
    ts: TS,
    ...overrides,
  };
}

function image(url: string, caption: string | null): MediaItem {
  return { kind: 'image', url, caption, filename: null };
}

function link(url: string, caption: string | null): MediaItem {
  return { kind: 'link', url, caption, filename: null };
}

function reply(text: string, extra: Partial<CardOption> = {}): CardOption {
  return { kind: 'reply', text, description: null, id: null, ...extra } as CardOption;
}

function linkOption(label: string, url: string): CardOption {
  return { kind: 'link', label, url };
}

function renderCard(item: MediaCardItem, onSend: SendReply = vi.fn(), locked = false) {
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

  it('renders no text bubble for a media-only card whose text is empty', () => {
    const { container } = renderCard(
      card({ text: '', media: [image('https://example.com/a.png', 'Item A')] }),
    );

    // No prose element at all — a caption-less media card shows just its media.
    expect(container.querySelector('.tcw-prose')).toBeNull();
    expect(screen.getByRole('img', { name: 'Item A' })).toBeInTheDocument();
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

  // -- new media kinds ---------------------------------------------------------

  it('renders a document as a download card labelled by its filename', () => {
    const doc: MediaItem = {
      kind: 'document',
      url: 'https://example.com/r.pdf',
      caption: 'Q3 report',
      filename: 'report.pdf',
    };
    renderCard(card({ media: [doc] }));

    const anchor = screen.getByRole('link', { name: /report\.pdf/ });
    expect(anchor).toHaveAttribute('href', 'https://example.com/r.pdf');
    expect(anchor).toHaveAttribute('download', 'report.pdf');
    expect(anchor).toHaveAttribute('target', '_blank');
    expect(anchor).toHaveAttribute('rel', 'noreferrer noopener');
    // The caption rides as a secondary line, and the download affordance is present.
    expect(screen.getByText('Q3 report')).toBeInTheDocument();
    expect(screen.getByText('Download')).toBeInTheDocument();
  });

  it('labels a filename-less document with its caption, and a bare one with its url', () => {
    renderCard(
      card({
        media: [
          {
            kind: 'document',
            url: 'https://example.com/x.pdf',
            caption: 'The brief',
            filename: null,
          },
        ],
      }),
    );
    expect(screen.getByRole('link', { name: /The brief/ })).toBeInTheDocument();

    cleanup();
    renderCard(
      card({
        media: [
          { kind: 'document', url: 'https://example.com/y.pdf', caption: null, filename: null },
        ],
      }),
    );
    expect(screen.getByRole('link', { name: /y\.pdf/ })).toBeInTheDocument();
  });

  it('renders a video as a native player with its source and its caption', () => {
    const { container } = renderCard(
      card({
        media: [
          { kind: 'video', url: 'https://example.com/clip.mp4', caption: 'A clip', filename: null },
        ],
      }),
    );
    const video = container.querySelector('video');
    expect(video).not.toBeNull();
    expect(video).toHaveAttribute('src', 'https://example.com/clip.mp4');
    expect(video).toHaveAttribute('controls');
    expect(screen.getByText('A clip').tagName).toBe('FIGCAPTION');
  });

  it('renders audio as a native player with its source', () => {
    const { container } = renderCard(
      card({
        media: [
          { kind: 'audio', url: 'https://example.com/note.mp3', caption: null, filename: null },
        ],
      }),
    );
    const audio = container.querySelector('audio');
    expect(audio).not.toBeNull();
    expect(audio).toHaveAttribute('src', 'https://example.com/note.mp3');
    expect(audio).toHaveAttribute('controls');
  });

  // -- typed options -----------------------------------------------------------

  it('sends a reply chip text as a regular visitor message when tapped', async () => {
    const user = userEvent.setup();
    const { onSend } = renderCard(card({ options: [reply('Item A'), reply('Item B')] }));

    await user.click(screen.getByRole('button', { name: 'Item A' }));

    expect(onSend).toHaveBeenCalledWith('Item A', null);
  });

  it('rides the tapped reply option authored id back on the send', async () => {
    const user = userEvent.setup();
    const { onSend } = renderCard(card({ options: [reply('Item A', { id: 'opt-a' })] }));

    await user.click(screen.getByRole('button', { name: /Item A/ }));

    expect(onSend).toHaveBeenCalledWith('Item A', 'opt-a');
  });

  it('shows a reply option description as a secondary line', () => {
    renderCard(card({ options: [reply('Book now', { description: 'the fast lane' })] }));

    expect(screen.getByText('Book now')).toBeInTheDocument();
    expect(screen.getByText('the fast lane')).toBeInTheDocument();
  });

  it('renders a link option as an anchor that opens the url in a new tab, distinct from a chip', () => {
    renderCard(card({ options: [linkOption('Read more', 'https://example.com/more')] }));

    const anchor = screen.getByRole('link', { name: /Read more/ });
    expect(anchor).toHaveAttribute('href', 'https://example.com/more');
    expect(anchor).toHaveAttribute('target', '_blank');
    // The SDK's link button pins a safe rel and marks it external.
    expect(anchor.getAttribute('rel')).toContain('noopener');
    expect(anchor).toHaveClass('tcw-link-option');
  });

  it('keeps a link action openable even when the session is locked (it sends nothing)', () => {
    renderCard(
      card({ options: [linkOption('Read more', 'https://example.com/more')] }),
      vi.fn(),
      true,
    );

    expect(screen.getByRole('link', { name: /Read more/ })).toHaveAttribute(
      'href',
      'https://example.com/more',
    );
  });

  it('keeps every reply chip tappable after a tap — no claim or settle', async () => {
    const user = userEvent.setup();
    const { onSend } = renderCard(card({ options: [reply('Item A'), reply('Item B')] }));

    await user.click(screen.getByRole('button', { name: 'Item A' }));
    await user.click(screen.getByRole('button', { name: 'Item A' }));

    expect(screen.getByRole('button', { name: 'Item A' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Item B' })).toBeEnabled();
    expect(onSend).toHaveBeenCalledTimes(2);
  });

  it('renders duplicate reply labels as distinct chips without a key collision', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      renderCard(card({ options: [reply('Pick'), reply('Pick')] }));

      expect(screen.getAllByRole('button', { name: 'Pick' })).toHaveLength(2);
      const warned = spy.mock.calls.some(
        (call) => typeof call[0] === 'string' && call[0].includes('two children with the same key'),
      );
      expect(warned).toBe(false);
    } finally {
      spy.mockRestore();
    }
  });

  it('disables every reply chip when the session is locked', () => {
    renderCard(card({ options: [reply('Item A'), reply('Item B')] }), vi.fn(), true);

    expect(screen.getByRole('button', { name: 'Item A' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Item B' })).toBeDisabled();
  });

  // -- sections ----------------------------------------------------------------

  it('renders a sectioned reply list with its section titles and rows', async () => {
    const user = userEvent.setup();
    const sections: OptionSection[] = [
      {
        title: 'Today',
        rows: [
          { kind: 'reply', text: '09:00', description: null, id: 't-9' },
          { kind: 'reply', text: '10:00', description: null, id: null },
        ],
      },
      { title: 'Tomorrow', rows: [{ kind: 'reply', text: '11:00', description: null, id: null }] },
    ];
    const { onSend } = renderCard(card({ sections }));

    expect(screen.getByText('Today')).toBeInTheDocument();
    expect(screen.getByText('Tomorrow')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '09:00' }));
    expect(onSend).toHaveBeenCalledWith('09:00', 't-9');
  });

  // -- header + footer ---------------------------------------------------------

  it('renders a media header above the body and a footer as trailing text', () => {
    const { container } = renderCard(
      card({
        header: image('https://example.com/banner.png', 'Banner'),
        footer: 'Powered by TAI',
        options: [reply('Item A')],
      }),
    );

    expect(screen.getByRole('img', { name: 'Banner' })).toBeInTheDocument();
    const footer = screen.getByText('Powered by TAI');
    expect(footer).toHaveClass('tcw-media-footer');
    // The header image renders before the prose in document order.
    const imgIndex = Array.from(container.querySelectorAll('*')).indexOf(
      container.querySelector('.tcw-media-image') as Element,
    );
    const proseIndex = Array.from(container.querySelectorAll('*')).indexOf(
      container.querySelector('.tcw-prose') as Element,
    );
    expect(imgIndex).toBeLessThan(proseIndex);
  });

  // -- location ----------------------------------------------------------------

  it('renders a location as a map-pin with name/address, coordinates and an OSM link', () => {
    const location: LocationPoint = {
      latitude: 51.5074,
      longitude: -0.1278,
      name: 'London',
      address: 'Trafalgar Square',
    };
    renderCard(card({ text: '', location }));

    expect(screen.getByText('London')).toBeInTheDocument();
    expect(screen.getByText('Trafalgar Square')).toBeInTheDocument();
    expect(screen.getByText('51.50740, -0.12780')).toBeInTheDocument();
    const anchor = screen.getByRole('link', { name: 'View on map' });
    const href = anchor.getAttribute('href') ?? '';
    expect(href.startsWith('https://www.openstreetmap.org/')).toBe(true);
    expect(href).toContain('mlat=51.5074');
    expect(href).toContain('mlon=-0.1278');
    expect(anchor).toHaveAttribute('target', '_blank');
  });
});
