import { act, renderHook, waitFor } from '@testing-library/react';
import type { DragEvent } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { attachKind, formatBytes, imagesFromClipboard, useDropZone, useUploads } from '@/uploads';

const fetchMock = vi.fn();

const MEDIA_ID = 'M'.repeat(43);

function accepted(overrides: Record<string, unknown> = {}): Response {
  return new Response(
    JSON.stringify({
      data: {
        media_id: MEDIA_ID,
        kind: 'image',
        mime: 'image/png',
        size: 2048,
        filename: null,
        url: `/api/interactions/media/${MEDIA_ID}`,
        ...overrides,
      },
    }),
    { status: 200 },
  );
}

function refusal(status: number, code: string | null): Response {
  const body: { error: string; code?: string } = { error: 'no' };
  if (code !== null) body.code = code;
  return new Response(JSON.stringify(body), { status });
}

function image(name = 'photo.png'): File {
  return new File(['x'], name, { type: 'image/png' });
}

function mountShell(): void {
  document.body.innerHTML = '<div id="root" data-api-base="/api/channels/web"></div>';
}

beforeEach(() => {
  mountShell();
  vi.stubGlobal('fetch', fetchMock);
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  fetchMock.mockReset();
});

interface HookProps {
  identity: string;
  connected: boolean;
  maxAttachments: number | null;
  onSessionEnded: () => void;
}

function online(maxAttachments: number | null = 10, onSessionEnded: () => void = () => {}) {
  return renderHook((props: HookProps) => useUploads(props), {
    initialProps: { identity: 'site-alpha', connected: true, maxAttachments, onSessionEnded },
  });
}

describe('deriving a file kind', () => {
  it('reads the browser media type', () => {
    expect(attachKind(new File([''], 'a.png', { type: 'image/png' }))).toBe('image');
    expect(attachKind(new File([''], 'a.mp4', { type: 'video/mp4' }))).toBe('video');
    expect(attachKind(new File([''], 'a.mp3', { type: 'audio/mpeg' }))).toBe('audio');
    expect(attachKind(new File([''], 'a.pdf', { type: 'application/pdf' }))).toBe('document');
  });
});

describe('formatting a size', () => {
  it('scales to the largest readable unit', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2 KB');
    expect(formatBytes(1_500_000)).toBe('1.4 MB');
  });
});

describe('adding a file', () => {
  it('uploads it at once and exposes its media id when ready', async () => {
    fetchMock.mockResolvedValue(accepted());
    const { result } = online();

    act(() => result.current.addFiles([image()]));

    await waitFor(() => expect(result.current.items[0]?.status).toBe('ready'));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/channels/web/uploads');
    expect(init.method).toBe('POST');
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get('identity')).toBe('site-alpha');
    expect((init.body as FormData).get('file')).toBeInstanceOf(File);
    expect(result.current.readyMediaIds).toEqual([MEDIA_ID]);
    expect(result.current.anyInFlight).toBe(false);
  });

  it('previews an image from a client data URL, not the served url', async () => {
    fetchMock.mockResolvedValue(accepted());
    const { result } = online();

    act(() => result.current.addFiles([image()]));

    await waitFor(() => expect(result.current.items[0]?.previewUrl).toMatch(/^data:/));
    const preview = result.current.readyMedia[0];
    expect(preview?.kind).toBe('image');
    expect(preview?.url).toMatch(/^data:/);
  });

  it('holds the tray in flight until the upload settles', async () => {
    let resolve: (r: Response) => void = () => {};
    fetchMock.mockReturnValue(new Promise<Response>((r) => (resolve = r)));
    const { result } = online();

    act(() => result.current.addFiles([image()]));

    expect(result.current.anyInFlight).toBe(true);
    expect(result.current.items[0]?.status).toBe('uploading');
    await act(async () => {
      resolve(accepted());
    });
    await waitFor(() => expect(result.current.anyInFlight).toBe(false));
  });
});

describe('a rejected upload', () => {
  it.each<[number, string | null, string]>([
    [415, 'media_type_not_allowed', 'That file type is not supported.'],
    [413, 'media_too_large', 'That file is too large.'],
    // A body-limit 413 from the middleware carries no envelope code; the status alone
    // is enough to name it a size refusal.
    [413, null, 'That file is too large.'],
    [503, 'media_store_unavailable', "Attachments aren't available right now."],
    [400, 'media_read_failed', 'Upload failed — tap to retry.'],
    [400, 'invalid_upload', 'Upload failed — tap to retry.'],
  ])('maps HTTP %i / %s to its inline reason', async (status, code, reason) => {
    fetchMock.mockResolvedValue(refusal(status, code));
    const { result } = online();

    act(() => result.current.addFiles([image()]));

    await waitFor(() => expect(result.current.items[0]?.status).toBe('failed'));
    expect(result.current.items[0]?.reason).toBe(reason);
    expect(result.current.announcement).toContain('failed');
    expect(result.current.readyMediaIds).toEqual([]);
  });

  it('ends the session when the door no longer resolves the visitor, wearing the reload copy', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: 'no session', code: 'session_missing' }), {
        status: 401,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const onSessionEnded = vi.fn();
    const { result } = online(10, onSessionEnded);

    act(() => result.current.addFiles([image()]));
    await waitFor(() => expect(result.current.items[0]?.status).toBe('failed'));

    expect(result.current.items[0]?.reason).toBe(
      'Your chat session ended — reload the page to start a new one.',
    );
    expect(onSessionEnded).toHaveBeenCalledTimes(1);
  });

  it('leaves the session alone on any other refusal', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: 'too big', code: 'media_too_large' }), {
        status: 413,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const onSessionEnded = vi.fn();
    const { result } = online(10, onSessionEnded);

    act(() => result.current.addFiles([image()]));
    await waitFor(() => expect(result.current.items[0]?.status).toBe('failed'));

    expect(onSessionEnded).not.toHaveBeenCalled();
  });

  it('re-issues the upload on retry', async () => {
    fetchMock.mockResolvedValueOnce(refusal(400, 'media_read_failed'));
    const { result } = online();
    act(() => result.current.addFiles([image()]));
    await waitFor(() => expect(result.current.items[0]?.status).toBe('failed'));

    fetchMock.mockResolvedValueOnce(accepted());
    act(() => result.current.retry(result.current.items[0]!.id));

    await waitFor(() => expect(result.current.items[0]?.status).toBe('ready'));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe('cancelling and removing', () => {
  it('aborts an in-flight upload and drops the item', async () => {
    fetchMock.mockImplementation(
      (_url, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () =>
            reject(new DOMException('aborted', 'AbortError')),
          );
        }),
    );
    const { result } = online();
    act(() => result.current.addFiles([image()]));
    const id = result.current.items[0]!.id;

    act(() => result.current.remove(id));

    await waitFor(() => expect(result.current.items).toHaveLength(0));
  });
});

describe('the optimistic preview media', () => {
  it('uses the client data URL for an image', async () => {
    fetchMock.mockResolvedValue(accepted());
    const { result } = online();
    act(() => result.current.addFiles([image()]));

    await waitFor(() => expect(result.current.items[0]?.previewUrl).toMatch(/^data:/));
    expect(result.current.readyMedia[0]?.kind).toBe('image');
    expect(result.current.readyMedia[0]?.url).toMatch(/^data:/);
  });

  it('shows the name-only chip for an image whose preview could not be read', async () => {
    fetchMock.mockResolvedValue(accepted());
    // A reader that fails: the tray shows no thumbnail, and the optimistic bubble must
    // not point an image at an empty source.
    class FailingReader {
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      result: string | ArrayBuffer | null = null;
      readAsDataURL(): void {
        queueMicrotask(() => this.onerror?.());
      }
    }
    vi.stubGlobal('FileReader', FailingReader);
    const { result } = online();

    act(() => result.current.addFiles([image('photo.png')]));
    await waitFor(() => expect(result.current.items[0]?.status).toBe('ready'));

    expect(result.current.items[0]?.previewUrl).toBeNull();
    expect(result.current.readyMedia).toEqual([
      { kind: 'document', url: '', caption: null, filename: 'photo.png' },
    ]);
  });

  it('shows a name-only chip for a non-image kind, never the served url that 404s pre-bind', async () => {
    fetchMock.mockResolvedValue(accepted({ kind: 'video', mime: 'video/mp4' }));
    const { result } = online();
    const clip = new File(['x'], 'clip.mp4', { type: 'video/mp4' });
    act(() => result.current.addFiles([clip]));

    await waitFor(() => expect(result.current.items[0]?.status).toBe('ready'));
    const preview = result.current.readyMedia[0];
    // A document-card chip (filename, no player), not a <video> pointed at a served
    // url that 404s until the message binds the id.
    expect(preview?.kind).toBe('document');
    expect(preview?.url).toBe('');
    expect(preview?.filename).toBe('clip.mp4');
  });
});

describe('the per-message cap', () => {
  it('refuses files past the cap and announces the limit', () => {
    fetchMock.mockResolvedValue(accepted());
    const { result } = online(2);

    act(() => result.current.addFiles([image('a.png'), image('b.png'), image('c.png')]));

    expect(result.current.items).toHaveLength(2);
    expect(result.current.announcement).toBe('You can attach up to 2 files per message.');
    expect(result.current.atCap).toBe(true);
  });

  it('refuses an add once the tray is already full', () => {
    fetchMock.mockResolvedValue(accepted());
    const { result } = online(1);

    act(() => result.current.addFiles([image('a.png')]));
    expect(result.current.atCap).toBe(true);

    act(() => result.current.addFiles([image('b.png')]));

    expect(result.current.items).toHaveLength(1);
    expect(result.current.announcement).toBe('You can attach up to 1 file per message.');
  });

  it('announces the cap on request, for an attach attempt made at the cap', () => {
    const { result } = online(3);

    act(() => result.current.announceCap());

    expect(result.current.announcement).toBe('You can attach up to 3 files per message.');
  });

  it('announces nothing for a cap request when the shell advertised no cap', () => {
    const { result } = online(null);

    act(() => result.current.announceCap());

    expect(result.current.announcement).toBe('');
  });

  it('applies no client cap when the shell advertised none', () => {
    fetchMock.mockResolvedValue(accepted());
    const { result } = online(null);

    act(() => result.current.addFiles([image('a.png'), image('b.png'), image('c.png')]));

    expect(result.current.items).toHaveLength(3);
    expect(result.current.atCap).toBe(false);
  });
});

describe('removing only the ids a send carried', () => {
  it('drops the sent ready items and leaves a failed one behind', async () => {
    fetchMock
      .mockResolvedValueOnce(accepted({ media_id: 'A'.repeat(43) }))
      .mockResolvedValueOnce(refusal(415, 'media_type_not_allowed'));
    const { result } = online();

    act(() => result.current.addFiles([image('ok.png'), image('bad.png')]));
    await waitFor(() => expect(result.current.items[0]?.status).toBe('ready'));
    await waitFor(() => expect(result.current.items[1]?.status).toBe('failed'));

    act(() => result.current.removeIds(result.current.readyMediaIds));

    expect(result.current.items).toHaveLength(1);
    expect(result.current.items[0]?.status).toBe('failed');
    expect(result.current.readyMediaIds).toEqual([]);
  });
});

describe('offline', () => {
  it('queues an add while the connection is down and uploads it on reconnect', async () => {
    fetchMock.mockResolvedValue(accepted());
    const { result, rerender } = renderHook((props: HookProps) => useUploads(props), {
      initialProps: {
        identity: 'site-alpha',
        connected: false,
        maxAttachments: 10,
        onSessionEnded: () => {},
      },
    });

    act(() => result.current.addFiles([image()]));

    expect(result.current.items[0]?.status).toBe('pending');
    expect(result.current.items[0]?.reason).toBe(
      "You're offline — the upload will retry when you reconnect.",
    );
    expect(fetchMock).not.toHaveBeenCalled();

    rerender({
      identity: 'site-alpha',
      connected: true,
      maxAttachments: 10,
      onSessionEnded: () => {},
    });

    await waitFor(() => expect(result.current.items[0]?.status).toBe('ready'));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

function dragEvent(files: File[] = [], types: string[] = ['Files']): DragEvent<HTMLElement> {
  return {
    preventDefault: vi.fn(),
    dataTransfer: { types, files, dropEffect: '' },
  } as unknown as DragEvent<HTMLElement>;
}

describe('the drop zone', () => {
  it('highlights on a file drag and hands dropped files over', () => {
    const onFiles = vi.fn();
    const { result } = renderHook(() => useDropZone(onFiles, true));

    act(() => result.current.onDragEnter(dragEvent()));
    expect(result.current.active).toBe(true);

    act(() => result.current.onDragOver(dragEvent()));
    const file = image();
    act(() => result.current.onDrop(dragEvent([file])));

    expect(onFiles).toHaveBeenCalledWith([file]);
    expect(result.current.active).toBe(false);
  });

  it('drops the highlight when the drag leaves', () => {
    const { result } = renderHook(() => useDropZone(vi.fn(), true));

    act(() => result.current.onDragEnter(dragEvent()));
    act(() => result.current.onDragLeave(dragEvent()));

    expect(result.current.active).toBe(false);
  });

  it('ignores a drag that carries no files', () => {
    const onFiles = vi.fn();
    const { result } = renderHook(() => useDropZone(onFiles, true));

    act(() => result.current.onDragEnter(dragEvent([], ['text/plain'])));
    expect(result.current.active).toBe(false);
  });

  it('ignores a drop while disabled', () => {
    const onFiles = vi.fn();
    const { result } = renderHook(() => useDropZone(onFiles, false));

    act(() => result.current.onDrop(dragEvent([image()])));

    expect(onFiles).not.toHaveBeenCalled();
  });
});

describe('reading images off a paste', () => {
  it('keeps only image files', () => {
    const png = image('clip.png');
    const items = [
      { kind: 'file', type: 'image/png', getAsFile: () => png },
      { kind: 'string', type: 'text/plain', getAsFile: () => null },
      { kind: 'file', type: 'application/pdf', getAsFile: () => new File(['x'], 'a.pdf') },
    ] as unknown as DataTransferItemList;

    expect(imagesFromClipboard(items)).toEqual([png]);
  });
});

describe('clearing the tray', () => {
  it('drops every item', async () => {
    fetchMock.mockResolvedValue(accepted());
    const { result } = online();
    act(() => result.current.addFiles([image()]));
    await waitFor(() => expect(result.current.items[0]?.status).toBe('ready'));

    act(() => result.current.clear());

    expect(result.current.items).toHaveLength(0);
    expect(result.current.readyMediaIds).toEqual([]);
  });
});
