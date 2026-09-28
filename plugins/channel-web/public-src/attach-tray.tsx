/**
 * The compose tray: the attachments being prepared for the next message, shown
 * above the composer input with a preview, per-file progress, and remove / cancel /
 * retry controls.
 *
 * Each item's state is spelled out for a screen reader as well as the eye: a shared
 * `role="status"` live line announces the latest change (uploading / ready / failed),
 * every control carries an `aria-label`, and Escape while an item's control is focused
 * cancels or removes that item. An image previews from a client `data:` URL; every
 * other kind shows a labelled glyph chip.
 */
import { Button, CloseIcon, FolderIcon, ProgressBar, XCircleIcon } from '@tai42/studio-sdk';
import type { KeyboardEvent, ReactElement } from 'react';

import { type AttachItem, formatBytes } from '@/uploads';

/** Escape on any of an item's focused controls cancels/removes it — the keyboard
 * equivalent of the remove/cancel button. */
type OnEscape = (event: KeyboardEvent<HTMLButtonElement>) => void;

export interface AttachTrayProps {
  readonly items: readonly AttachItem[];
  /** The latest lifecycle change, read out through the tray's live region. */
  readonly announcement: string;
  /** Aborts an in-flight upload (a cancel) or drops a ready/failed one (a remove). */
  readonly onRemove: (id: string) => void;
  readonly onRetry: (id: string) => void;
  /** No message can be sent (an ended session): the tray's controls are stopped
   * with the rest of the composer. */
  readonly disabled: boolean;
}

export function AttachTray({
  items,
  announcement,
  onRemove,
  onRetry,
  disabled,
}: AttachTrayProps): ReactElement | null {
  if (items.length === 0) {
    // The live region stays mounted so a "ready"/"failed" for the last item is still
    // announced as the tray empties; nothing else renders.
    return (
      <p className="tai-visually-hidden" role="status" aria-live="polite">
        {announcement}
      </p>
    );
  }
  return (
    <div className="tcw-compose-tray" data-testid="compose-tray">
      {items.map((item) => (
        <AttachItemRow
          key={item.id}
          item={item}
          onRemove={onRemove}
          onRetry={onRetry}
          disabled={disabled}
        />
      ))}
      <p className="tai-visually-hidden" role="status" aria-live="polite">
        {announcement}
      </p>
    </div>
  );
}

function AttachItemRow({
  item,
  onRemove,
  onRetry,
  disabled,
}: {
  readonly item: AttachItem;
  readonly onRemove: (id: string) => void;
  readonly onRetry: (id: string) => void;
  readonly disabled: boolean;
}): ReactElement {
  const onEscape: OnEscape = (event) => {
    if (event.key !== 'Escape') return;
    event.stopPropagation();
    onRemove(item.id);
  };
  return (
    <div className="tcw-attach-item" data-testid="attach-item" data-status={item.status}>
      <span className="tcw-attach-preview" aria-hidden="true">
        {item.kind === 'image' && item.previewUrl !== null ? (
          <img className="tcw-attach-thumb" src={item.previewUrl} alt="" />
        ) : (
          <FolderIcon />
        )}
      </span>
      <span className="tcw-attach-body">
        <span className="tcw-attach-name">{item.filename}</span>
        <AttachState item={item} onRetry={onRetry} onEscape={onEscape} disabled={disabled} />
      </span>
      <AttachControls item={item} onRemove={onRemove} onEscape={onEscape} disabled={disabled} />
    </div>
  );
}

/** The line under the filename: the size when ready, an indeterminate bar while
 * uploading, or the failure/offline reason (a retry button on a failed item). */
function AttachState({
  item,
  onRetry,
  onEscape,
  disabled,
}: {
  readonly item: AttachItem;
  readonly onRetry: (id: string) => void;
  readonly onEscape: OnEscape;
  readonly disabled: boolean;
}): ReactElement {
  if (item.status === 'uploading') {
    return (
      <span className="tcw-attach-progress">
        <ProgressBar />
      </span>
    );
  }
  if (item.status === 'failed' && item.reason !== null) {
    return (
      <Button
        type="button"
        variant="ghost"
        className="tcw-attach-error"
        onClick={() => onRetry(item.id)}
        onKeyDown={onEscape}
        disabled={disabled}
        // The reason is the button's visible text, which an aria-label would override,
        // so it is spelled into the label too — a screen-reader user learns WHY the
        // retry is offered, not only that it is, after the live announcement is gone.
        aria-label={`Retry ${item.filename} — ${item.reason}`}
      >
        {item.reason}
      </Button>
    );
  }
  if (item.reason !== null) {
    return <span className="tcw-attach-meta tcw-attach-meta--wait">{item.reason}</span>;
  }
  return <span className="tcw-attach-meta">{formatBytes(item.size)}</span>;
}

/** The trailing control: cancel while uploading, remove otherwise. */
function AttachControls({
  item,
  onRemove,
  onEscape,
  disabled,
}: {
  readonly item: AttachItem;
  readonly onRemove: (id: string) => void;
  readonly onEscape: OnEscape;
  readonly disabled: boolean;
}): ReactElement {
  const uploading = item.status === 'uploading';
  return (
    <Button
      type="button"
      variant="ghost"
      className="tcw-attach-remove"
      onClick={() => onRemove(item.id)}
      onKeyDown={onEscape}
      disabled={disabled}
      aria-label={uploading ? 'Cancel upload' : `Remove ${item.filename}`}
    >
      {uploading ? <XCircleIcon aria-hidden="true" /> : <CloseIcon aria-hidden="true" />}
    </Button>
  );
}
