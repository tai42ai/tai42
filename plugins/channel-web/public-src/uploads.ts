/**
 * The compose tray's upload engine: one hook that holds the attachments being
 * prepared for the next message and drives each one's `POST /uploads` on its own.
 *
 * A file is uploaded the moment it is added — not on send — so it is already a
 * PENDING seam item, with its `media_id` in hand, by the time the visitor sends;
 * the message then binds it. Each upload has its own {@link AbortController}, so a
 * cancel or a remove aborts just that one. An add made while the stream is down is
 * QUEUED and retried when the connection returns, rather than failed.
 *
 * A failure is never silent: every rejected upload lands in `failed` (or `pending`
 * while offline) wearing the reason the door's `code` maps to, and each keeps a
 * retry. Image previews use a client `data:` URL (a `FileReader` read), because the
 * served url 404s until the message binds the id and a `blob:` url is outside the
 * page CSP.
 */
import type { Dispatch, DragEvent, SetStateAction } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ChatApiError, isSessionMissing, uploadAttachment } from '@/api';
import type { MediaItem, MediaKind } from '@/transcript-model';

/** Where one tray attachment is in its lifecycle: queued offline, uploading, ready
 * to be referenced on the wire, or failed. */
export type AttachStatus = 'pending' | 'uploading' | 'ready' | 'failed';

/** One attachment being prepared for the next message. */
export interface AttachItem {
  readonly id: string;
  /** The picked file, kept so a retry re-uploads it and an image can be previewed. */
  readonly file: File;
  readonly filename: string;
  readonly size: number;
  /** Derived from the browser's declared type; the seam's own sniff decides the
   * authoritative kind on the wire. */
  readonly kind: MediaKind;
  readonly status: AttachStatus;
  /** A `data:` URL for an image, read client-side for the preview; `null` otherwise. */
  readonly previewUrl: string | null;
  /** The visitor-facing reason, on a `failed` or offline-`pending` item. */
  readonly reason: string | null;
  /** The seam media id, once the upload is `ready`. */
  readonly mediaId: string | null;
  /** The same-origin served url, once `ready` — used only by the post-bind frame. */
  readonly url: string | null;
}

const TOO_LARGE = 'That file is too large.';
const NOT_SUPPORTED = 'That file type is not supported.';
const STORE_UNAVAILABLE = "Attachments aren't available right now.";
const FAILED_RETRY = 'Upload failed — tap to retry.';
const OFFLINE = "You're offline — the upload will retry when you reconnect.";

/** The refusal shown when a selection would take the tray past the per-message cap
 * the shell advertised. The messages door's 422 is the authoritative backstop; this
 * is the earlier, friendlier stop so the visitor is not sent to a failed send. */
export function attachCapCopy(max: number): string {
  return `You can attach up to ${max} ${max === 1 ? 'file' : 'files'} per message.`;
}

let attachIdSeq = 0;
function nextAttachId(): string {
  attachIdSeq += 1;
  return `attach-${attachIdSeq.toString(36)}`;
}

/** The media kind a browser file maps to for the preview, from its declared type. */
export function attachKind(file: File): MediaKind {
  if (file.type.startsWith('image/')) return 'image';
  if (file.type.startsWith('video/')) return 'video';
  if (file.type.startsWith('audio/')) return 'audio';
  return 'document';
}

/** A file size in the largest unit that keeps it readable. */
export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = size / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 || Number.isInteger(value) ? 0 : 1)} ${units[unit]}`;
}

/** The inline copy for one upload failure, mapped from the door's `code` (then its
 * status), so the visitor reads why it failed rather than a raw diagnostic. */
function reasonFor(error: unknown): string {
  if (error instanceof ChatApiError) {
    // The session is gone: the door's own reload copy is the reason, and the page
    // enters its ended state (see `runUpload`).
    if (isSessionMissing(error)) return error.message;
    if (error.code === 'media_too_large') return TOO_LARGE;
    if (error.code === 'media_type_not_allowed') return NOT_SUPPORTED;
    if (error.code === 'media_store_unavailable') return STORE_UNAVAILABLE;
    if (error.status === 413) return TOO_LARGE;
  }
  return FAILED_RETRY;
}

/** Read an image as a `data:` URL for the client-side preview; resolves `null` when
 * the read fails (the tray simply shows no thumbnail rather than a broken one). */
function readImagePreview(file: File): Promise<string | null> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : null);
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(file);
  });
}

/** The optimistic-bubble media item for a ready attachment. The served `url` 404s
 * until the message binds the id, so it is NEVER used here: an image shows its client
 * `data:` URL, and every other kind — and an image whose preview could not be read —
 * shows a name-only chip (rendered as a document card) rather than a player or an
 * image pointed at a url that is not yet fetchable. The retiring `chat.message` frame
 * replaces this with the real served ref moments later. */
function toPreviewMedia(item: AttachItem): MediaItem {
  if (item.kind === 'image' && item.previewUrl !== null) {
    return { kind: 'image', url: item.previewUrl, caption: null, filename: null };
  }
  return { kind: 'document', url: '', caption: null, filename: item.filename };
}

interface UploadCtx {
  readonly identity: string;
  readonly controllers: Map<string, AbortController>;
  readonly connectedRef: { current: boolean };
  readonly patch: (id: string, partial: Partial<AttachItem>) => void;
  readonly announce: (message: string) => void;
  /** The visitor's session no longer resolves — the page enters its ended state. */
  readonly onSessionEnded: () => void;
}

/** The context the upload workers write through — the item patcher and the live-region
 * announcer — bound once per identity so the callbacks built on it stay stable. */
function useUploadCtx(
  identity: string,
  controllers: Map<string, AbortController>,
  connectedRef: { current: boolean },
  setItems: Dispatch<SetStateAction<readonly AttachItem[]>>,
  announce: (message: string) => void,
  onSessionEnded: () => void,
): UploadCtx {
  const patch = useCallback(
    (id: string, partial: Partial<AttachItem>): void => {
      setItems((prev) => prev.map((item) => (item.id === id ? { ...item, ...partial } : item)));
    },
    [setItems],
  );
  return useMemo(
    () => ({ identity, controllers, connectedRef, patch, announce, onSessionEnded }),
    [identity, controllers, connectedRef, patch, announce, onSessionEnded],
  );
}

/** Drive one attachment's upload to `ready`, or to `failed`/offline-`pending` — never
 * a silent drop. An abort (cancel/remove) is the one outcome that patches nothing:
 * the item is already gone. */
function runUpload(item: AttachItem, ctx: UploadCtx): void {
  const controller = new AbortController();
  ctx.controllers.set(item.id, controller);
  ctx.patch(item.id, { status: 'uploading', reason: null });
  ctx.announce(`Uploading ${item.filename}…`);
  uploadAttachment(ctx.identity, item.file, controller.signal).then(
    (accepted) => {
      ctx.controllers.delete(item.id);
      ctx.patch(item.id, { status: 'ready', mediaId: accepted.media_id, url: accepted.url });
      ctx.announce(`${item.filename} ready`);
    },
    (error: unknown) => {
      ctx.controllers.delete(item.id);
      if (controller.signal.aborted) return;
      if (!ctx.connectedRef.current) {
        ctx.patch(item.id, { status: 'pending', reason: OFFLINE });
        return;
      }
      const reason = reasonFor(error);
      ctx.patch(item.id, { status: 'failed', reason });
      ctx.announce(`${item.filename} failed — ${reason}`);
      // A refused session ends the page the same way a refused send does; a retry of
      // the upload could only be refused again.
      if (isSessionMissing(error)) ctx.onSessionEnded();
    },
  );
}

/** Add picked/dropped/pasted files to the tray, each uploaded at once. Anything past
 * the per-message cap is refused before upload (the tray never holds more ids than one
 * message may carry); a cap of `null` means the shell named none, so every file is
 * accepted and the door's 422 is the only stop. The cap refusal is announced LAST so
 * the actionable message, not a per-file "Uploading…", is what the live region carries.
 */
function performAdd(
  files: readonly File[],
  maxAttachments: number | null,
  ctx: UploadCtx,
  currentCount: number,
  setItems: Dispatch<SetStateAction<readonly AttachItem[]>>,
): void {
  const accepted =
    maxAttachments === null ? files : files.slice(0, Math.max(0, maxAttachments - currentCount));
  for (const file of accepted) {
    const online = ctx.connectedRef.current;
    const item = makeAttachItem(file, online);
    setItems((prev) => [...prev, item]);
    if (item.kind === 'image')
      void readImagePreview(file).then((url) => ctx.patch(item.id, { previewUrl: url }));
    if (online) runUpload(item, ctx);
    else ctx.announce(`${file.name} — ${OFFLINE}`);
  }
  if (maxAttachments !== null && accepted.length < files.length)
    ctx.announce(attachCapCopy(maxAttachments));
}

/** The compose tray: the attachments being prepared, and the doors that add, remove,
 * retry and clear them. */
export interface Uploads {
  readonly items: readonly AttachItem[];
  readonly addFiles: (files: readonly File[]) => void;
  /** Abort an in-flight upload (a cancel) or drop a ready/failed one (a remove). */
  readonly remove: (id: string) => void;
  /** Drop just the items whose `media_id` is in the given set — the ones a send
   * carried — leaving `failed`, offline-`pending` and still-`uploading` items in the
   * tray with their retry/queue affordance. */
  readonly removeIds: (mediaIds: readonly string[]) => void;
  readonly retry: (id: string) => void;
  /** Drop the whole tray, aborting anything in flight (a new conversation / reset). */
  readonly clear: () => void;
  /** The `media_id`s of every ready attachment, in tray order — the send's wire ids. */
  readonly readyMediaIds: readonly string[];
  /** The ready attachments as preview media items, for the optimistic bubble. */
  readonly readyMedia: readonly MediaItem[];
  /** An upload is still in flight, so the send control is held until it settles. */
  readonly anyInFlight: boolean;
  /** The tray already holds the most files one message may carry, so the attach
   * control is stopped rather than letting a doomed selection be made. Always false
   * when the shell advertised no cap. */
  readonly atCap: boolean;
  /** The most recent lifecycle change, announced through the tray's live region. */
  readonly announcement: string;
  /** Announces the cap refusal through the live region, for an attach attempt made
   * while the tray is at the cap — the control's tooltip does not show on touch. */
  readonly announceCap: () => void;
}

/** A fresh tray item for a picked file: uploading when online, queued otherwise. */
function makeAttachItem(file: File, online: boolean): AttachItem {
  return {
    id: nextAttachId(),
    file,
    filename: file.name,
    size: file.size,
    kind: attachKind(file),
    status: online ? 'uploading' : 'pending',
    previewUrl: null,
    reason: online ? null : OFFLINE,
    mediaId: null,
    url: null,
  };
}

/** The send-path projection of the tray: the ready ids and their preview media. */
function readyExposed(items: readonly AttachItem[]): {
  readonly readyMediaIds: string[];
  readonly readyMedia: MediaItem[];
} {
  const ready = items.filter((item) => item.status === 'ready');
  return {
    readyMediaIds: ready.flatMap((item) => (item.mediaId !== null ? [item.mediaId] : [])),
    readyMedia: ready.map(toPreviewMedia),
  };
}

export function useUploads(params: {
  identity: string;
  connected: boolean;
  /** The per-message attachment cap the shell advertised, or `null` when it named
   * none — in which case the tray applies no client-side cap and the messages door's
   * 422 is the sole enforcer. */
  maxAttachments: number | null;
  /** Called when an upload is refused because the visitor's session no longer
   * resolves; the page's session-ended handler, shared with the send path. */
  onSessionEnded: () => void;
}): Uploads {
  const { identity, connected, maxAttachments, onSessionEnded } = params;
  const [items, setItems] = useState<readonly AttachItem[]>([]);
  const [announcement, setAnnouncement] = useState('');
  const controllers = useRef(new Map<string, AbortController>());
  const connectedRef = useRef(connected);
  const itemsRef = useRef(items);
  itemsRef.current = items;

  const ctx = useUploadCtx(
    identity,
    controllers.current,
    connectedRef,
    setItems,
    setAnnouncement,
    onSessionEnded,
  );

  const addFiles = useCallback(
    (files: readonly File[]): void =>
      performAdd(files, maxAttachments, ctx, itemsRef.current.length, setItems),
    [ctx, maxAttachments],
  );

  const remove = useCallback((id: string): void => {
    controllers.current.get(id)?.abort();
    controllers.current.delete(id);
    setItems((prev) => prev.filter((item) => item.id !== id));
  }, []);

  const removeIds = useCallback((mediaIds: readonly string[]): void => {
    const drop = new Set(mediaIds);
    setItems((prev) =>
      prev.filter((item) => {
        if (item.mediaId === null || !drop.has(item.mediaId)) return true;
        controllers.current.get(item.id)?.abort();
        controllers.current.delete(item.id);
        return false;
      }),
    );
  }, []);

  const retry = useCallback(
    (id: string): void => {
      const item = itemsRef.current.find((candidate) => candidate.id === id);
      if (item !== undefined) runUpload(item, ctx);
    },
    [ctx],
  );

  const clear = useCallback((): void => {
    for (const controller of controllers.current.values()) controller.abort();
    controllers.current.clear();
    setItems([]);
  }, []);

  useEffect(() => {
    connectedRef.current = connected;
    if (!connected) return;
    for (const item of itemsRef.current) if (item.status === 'pending') runUpload(item, ctx);
  }, [connected, ctx]);

  const { readyMediaIds, readyMedia } = useMemo(() => readyExposed(items), [items]);
  const anyInFlight = useMemo(() => items.some((item) => item.status === 'uploading'), [items]);
  const atCap = maxAttachments !== null && items.length >= maxAttachments;
  const announceCap = useCallback(() => {
    if (maxAttachments !== null) setAnnouncement(attachCapCopy(maxAttachments));
  }, [maxAttachments]);

  return {
    items,
    addFiles,
    remove,
    removeIds,
    retry,
    clear,
    readyMediaIds,
    readyMedia,
    anyInFlight,
    atCap,
    announcement,
    announceCap,
  };
}

/** A drag-and-drop file target: the active-highlight flag and the handlers to spread
 * onto the drop region. Enter/leave are depth-counted so dragging over a child does
 * not flicker the highlight off; a drop while disabled (an ended session) is ignored. */
export interface DropZone {
  readonly active: boolean;
  readonly onDragEnter: (event: DragEvent<HTMLElement>) => void;
  readonly onDragOver: (event: DragEvent<HTMLElement>) => void;
  readonly onDragLeave: (event: DragEvent<HTMLElement>) => void;
  readonly onDrop: (event: DragEvent<HTMLElement>) => void;
}

function carriesFiles(event: DragEvent<HTMLElement>): boolean {
  return Array.from(event.dataTransfer.types).includes('Files');
}

export function useDropZone(onFiles: (files: readonly File[]) => void, enabled: boolean): DropZone {
  const [active, setActive] = useState(false);
  const depth = useRef(0);

  const onDragEnter = useCallback(
    (event: DragEvent<HTMLElement>): void => {
      if (!enabled || !carriesFiles(event)) return;
      event.preventDefault();
      depth.current += 1;
      setActive(true);
    },
    [enabled],
  );

  const onDragOver = useCallback(
    (event: DragEvent<HTMLElement>): void => {
      if (!enabled || !carriesFiles(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
    },
    [enabled],
  );

  const onDragLeave = useCallback((event: DragEvent<HTMLElement>): void => {
    if (!carriesFiles(event)) return;
    depth.current = Math.max(0, depth.current - 1);
    if (depth.current === 0) setActive(false);
  }, []);

  const onDrop = useCallback(
    (event: DragEvent<HTMLElement>): void => {
      if (!carriesFiles(event)) return;
      event.preventDefault();
      depth.current = 0;
      setActive(false);
      if (!enabled) return;
      const files = Array.from(event.dataTransfer.files);
      if (files.length > 0) onFiles(files);
    },
    [enabled, onFiles],
  );

  return { active, onDragEnter, onDragOver, onDragLeave, onDrop };
}

/** The image files carried on a paste, for the composer's clipboard capture. */
export function imagesFromClipboard(items: DataTransferItemList): File[] {
  const files: File[] = [];
  for (const item of Array.from(items)) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file !== null) files.push(file);
    }
  }
  return files;
}
