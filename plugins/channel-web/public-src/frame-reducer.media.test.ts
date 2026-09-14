import { describe, expect, it } from 'vitest';

import { applyFrame } from '@/frame-reducer';
import { EMPTY_MODEL } from '@/transcript-model';
import { TS, frame, fold, mediaFrame } from '@/frame-reducer.test-support';

describe('applyFrame: chat.media', () => {
  it('folds a media card with an image and its caption', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        media: [{ kind: 'image', url: 'https://example.com/a.png', caption: 'Item A' }],
      }),
    );

    expect(model.items[0]).toEqual({
      kind: 'media',
      id: 'md1',
      text: 'Here you go',
      media: [
        { kind: 'image', url: 'https://example.com/a.png', caption: 'Item A', filename: null },
      ],
      options: null,
      sections: null,
      header: null,
      footer: null,
      location: null,
      ts: TS,
    });
  });

  it('folds a media card that is text and tappable options only', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        options: [
          { kind: 'reply', text: 'Item A' },
          { kind: 'reply', text: 'Item B' },
        ],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      media: null,
      options: [
        { kind: 'reply', text: 'Item A', description: null, id: null },
        { kind: 'reply', text: 'Item B', description: null, id: null },
      ],
    });
  });

  it('folds reply and link options together, preserving id and description', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        options: [
          { kind: 'reply', text: 'Book', description: 'the fast lane', id: 'opt-1' },
          { kind: 'link', label: 'Read more', url: 'https://example.com/more' },
        ],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      options: [
        { kind: 'reply', text: 'Book', description: 'the fast lane', id: 'opt-1' },
        { kind: 'link', label: 'Read more', url: 'https://example.com/more' },
      ],
    });
  });

  it('folds a media card carrying an image and options together', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        media: [{ kind: 'image', url: 'https://example.com/a.png' }],
        options: [{ kind: 'reply', text: 'See all' }],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      media: [{ kind: 'image', url: 'https://example.com/a.png', caption: null, filename: null }],
      options: [{ kind: 'reply', text: 'See all', description: null, id: null }],
    });
  });

  it('folds a sectioned reply list', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        sections: [
          { title: 'Today', rows: [{ kind: 'reply', text: '09:00', id: 't-9' }] },
          { title: 'Tomorrow', rows: [{ kind: 'reply', text: '11:00' }] },
        ],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      options: null,
      sections: [
        { title: 'Today', rows: [{ kind: 'reply', text: '09:00', description: null, id: 't-9' }] },
        {
          title: 'Tomorrow',
          rows: [{ kind: 'reply', text: '11:00', description: null, id: null }],
        },
      ],
    });
  });

  it('folds a header, footer and location on an interactive card', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        header: { kind: 'image', url: 'https://example.com/banner.png', caption: 'Banner' },
        footer: 'Powered by TAI',
        location: { latitude: 51.5, longitude: -0.12, name: 'London', address: 'Trafalgar Square' },
        options: [{ kind: 'reply', text: 'Item A' }],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      header: {
        kind: 'image',
        url: 'https://example.com/banner.png',
        caption: 'Banner',
        filename: null,
      },
      footer: 'Powered by TAI',
      location: { latitude: 51.5, longitude: -0.12, name: 'London', address: 'Trafalgar Square' },
    });
  });

  it('folds a document with its filename and native video/audio media', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({
        media: [
          {
            kind: 'document',
            url: 'https://example.com/r.pdf',
            caption: 'Q3',
            filename: 'report.pdf',
          },
          { kind: 'video', url: 'https://example.com/clip.mp4' },
          { kind: 'audio', url: 'https://example.com/note.mp3' },
        ],
      }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      media: [
        {
          kind: 'document',
          url: 'https://example.com/r.pdf',
          caption: 'Q3',
          filename: 'report.pdf',
        },
        { kind: 'video', url: 'https://example.com/clip.mp4', caption: null, filename: null },
        { kind: 'audio', url: 'https://example.com/note.mp3', caption: null, filename: null },
      ],
    });
  });

  it('folds a content-only location card (blank text, no media/options)', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({ text: '', location: { latitude: 1, longitude: 2 } }),
    );

    expect(model.items[0]).toMatchObject({
      kind: 'media',
      text: '',
      media: null,
      options: null,
      location: { latitude: 1, longitude: 2, name: null, address: null },
    });
  });

  it('carries a link attachment as a validated http(s) url', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({ media: [{ kind: 'link', url: 'http://example.com/a', caption: 'Open A' }] }),
    );

    expect(model.items[0]).toMatchObject({
      media: [{ kind: 'link', url: 'http://example.com/a', caption: 'Open A', filename: null }],
    });
  });

  it('de-duplicates a redelivered media card on backlog replay', () => {
    const model = fold(
      EMPTY_MODEL,
      mediaFrame({ options: [{ kind: 'reply', text: 'Item A' }] }),
      mediaFrame({ options: [{ kind: 'reply', text: 'Item A' }] }),
    );

    expect(model.items).toHaveLength(1);
    expect(model.items[0]).toMatchObject({
      kind: 'media',
      options: [{ kind: 'reply', text: 'Item A', description: null, id: null }],
    });
  });

  it.each([
    [
      // The new known set (document/video/audio) is admitted; a kind outside it
      // still rejects rather than rendering as a blank attachment.
      'a media card with an unknown attachment kind',
      mediaFrame({ media: [{ kind: 'sticker', url: 'https://example.com/a.webp' }] }),
    ],
    [
      // A video/audio/document still obeys the https file discipline — an http
      // source the CSP would refuse taints the frame.
      'a media video whose url is not https',
      mediaFrame({ media: [{ kind: 'video', url: 'http://example.com/a.mp4' }] }),
    ],
    [
      // filename rides a document only; on any other kind it is off-contract.
      'a filename on a non-document media item',
      mediaFrame({
        media: [{ kind: 'image', url: 'https://example.com/a.png', filename: 'a.png' }],
      }),
    ],
    [
      'a media image whose url carries a user@ authority',
      mediaFrame({ media: [{ kind: 'image', url: 'https://user:pass@example.com/a.png' }] }),
    ],
    [
      'a media link whose url carries a user@ authority',
      mediaFrame({ media: [{ kind: 'link', url: 'https://user:pass@example.com/x' }] }),
    ],
    [
      'a media image whose url is not https',
      mediaFrame({ media: [{ kind: 'image', url: 'http://example.com/a.png' }] }),
    ],
    [
      'a media image whose url is relative rather than absolute',
      mediaFrame({ media: [{ kind: 'image', url: '/a.png' }] }),
    ],
    [
      'a media link whose url is neither http nor https',
      mediaFrame({ media: [{ kind: 'link', url: 'ftp://example.com/a' }] }),
    ],
    [
      'a media attachment whose caption is not a string',
      mediaFrame({ media: [{ kind: 'image', url: 'https://example.com/a.png', caption: 7 }] }),
    ],
    [
      'a media attachment whose caption is explicitly null',
      mediaFrame({ media: [{ kind: 'image', url: 'https://example.com/a.png', caption: null }] }),
    ],
    ['a reply option with blank text', mediaFrame({ options: [{ kind: 'reply', text: '  ' }] })],
    ['a bare-string option (the old flat shape)', mediaFrame({ options: ['Item A'] })],
    ['a media card with an empty options array', mediaFrame({ options: [] })],
    ['an option with no kind', mediaFrame({ options: [{ text: 'Item A' }] })],
    [
      'a reply option whose text is not a string',
      mediaFrame({ options: [{ kind: 'reply', text: 1 }] }),
    ],
    [
      'a link option whose url is not http(s)',
      mediaFrame({ options: [{ kind: 'link', label: 'x', url: 'ftp://example.com/a' }] }),
    ],
    [
      'a link option with a blank label',
      mediaFrame({ options: [{ kind: 'link', label: ' ', url: 'https://ex/a' }] }),
    ],
    ['a media card whose options is a non-array object', mediaFrame({ options: { a: 1 } })],
    [
      'options and sections together (two choice surfaces)',
      mediaFrame({
        options: [{ kind: 'reply', text: 'a' }],
        sections: [{ title: 'S', rows: [{ kind: 'reply', text: 'b' }] }],
      }),
    ],
    ['a section with empty rows', mediaFrame({ sections: [{ title: 'S', rows: [] }] })],
    [
      'a section with a blank title',
      mediaFrame({ sections: [{ title: ' ', rows: [{ kind: 'reply', text: 'b' }] }] }),
    ],
    [
      'a section row that is a link (rows are replies only)',
      mediaFrame({
        sections: [{ title: 'S', rows: [{ kind: 'link', label: 'x', url: 'https://ex/a' }] }],
      }),
    ],
    [
      'a header with no options or sections',
      mediaFrame({ header: { kind: 'image', url: 'https://example.com/a.png' } }),
    ],
    [
      'a link header (a header is display media, never a link)',
      mediaFrame({
        header: { kind: 'link', url: 'https://ex/a' },
        options: [{ kind: 'reply', text: 'a' }],
      }),
    ],
    ['a footer with no options or sections', mediaFrame({ footer: 'trailing' })],
    [
      'a location with an out-of-range latitude',
      mediaFrame({ location: { latitude: 91, longitude: 0 } }),
    ],
    [
      'a location with a non-number coordinate',
      mediaFrame({ location: { latitude: '51', longitude: 0 } }),
    ],
    [
      'a media card missing its text',
      frame('chat.media', {
        id: 'md1',
        direction: 'out',
        ts: TS,
        options: [{ kind: 'reply', text: 'Item A' }],
      }),
    ],
    [
      'a media card whose direction is not out',
      mediaFrame({ direction: 'in', options: [{ kind: 'reply', text: 'Item A' }] }),
    ],
    ['a media card with no content at all', mediaFrame({})],
    [
      'a media card whose media is not an array',
      mediaFrame({ media: { kind: 'image', url: 'https://example.com/a.png' } }),
    ],
  ])('surfaces %s as malformed rather than dropping it', (_label, bad) => {
    expect(applyFrame(EMPTY_MODEL, bad).kind).toBe('malformed');
  });
});
