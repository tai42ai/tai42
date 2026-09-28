/**
 * The message composer: a growing textarea plus its send control.
 *
 * Enter sends and Shift+Enter inserts a newline — the messenger convention — so
 * the textarea also carries an explicit send button for touch and for anyone
 * driving it from the keyboard alone. The field clears and takes focus back the
 * moment a send is handed off, so a fast typist never waits on the network.
 *
 * The field carries the message door's own character cap, so an over-long message
 * is stopped where it is being written rather than refused after it has been sent.
 * The remaining count appears as the cap comes into reach; like the question card's
 * countdown it is presentational, and what is SPOKEN changes only at coarse
 * thresholds so a keystroke cannot announce a keystroke.
 */
import { ArrowUpIcon, Button, FolderIcon, Textarea } from '@tai42/studio-sdk';
import type { ChangeEvent, ClipboardEvent, KeyboardEvent, ReactElement, Ref } from 'react';
import { useLayoutEffect, useRef } from 'react';

import { MAX_MESSAGE_CHARS } from '@/api';
import { AttachTray } from '@/attach-tray';
import { attachCapCopy, type AttachItem, imagesFromClipboard } from '@/uploads';

/** How tall the field may grow before it scrolls instead. */
const MAX_HEIGHT_PX = 160;

/** How close to the cap the remaining count becomes visible. */
const COUNTER_FROM = 200;

/**
 * What a screen reader is told about the room left, COARSE and empty until the cap
 * is in reach. The visible count changes on every keystroke, and announcing each
 * one would talk over the visitor as they type; this string only changes as the
 * room left crosses a threshold, and an unchanged string re-renders without
 * announcing again.
 */
export function charactersLeftAnnouncement(remaining: number): string {
  if (remaining > COUNTER_FROM) return '';
  if (remaining > 100) return '200 characters left';
  if (remaining > 0) return '100 characters left';
  return 'You have reached the message length limit';
}

export interface ComposerProps {
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly onSend: () => void;
  /** No message can be sent (an ended session) — the field says why and refuses. */
  readonly disabled: boolean;
  readonly placeholder: string;
  readonly inputRef: Ref<HTMLTextAreaElement>;
  /** The compose tray's pending attachments, rendered above the input. */
  readonly attachments: readonly AttachItem[];
  /** Add files picked, pasted or dropped for the next message to the tray. */
  readonly onAddFiles: (files: readonly File[]) => void;
  /** Abort an in-flight upload (a cancel) or drop a ready/failed one (a remove). */
  readonly onRemoveAttachment: (id: string) => void;
  readonly onRetryAttachment: (id: string) => void;
  /** The tray's live-region text, announced as an attachment's state changes. */
  readonly attachAnnouncement: string;
  /** An upload is still in flight — the send control waits until it settles, because
   * a half-uploaded file cannot be referenced. */
  readonly uploadsInFlight: boolean;
  /** The tray already holds the most files one message may carry, so the attach
   * control is stopped rather than letting a doomed selection be made. */
  readonly attachAtCap: boolean;
  /** The per-message attachment cap, for the at-cap message; `null` when the shell
   * advertised none (in which case the tray is never at a cap). */
  readonly maxAttachments: number | null;
  /** The attach control was used at the cap: the reason is announced, since the
   * control's tooltip reaches neither a touch nor a screen-reader user. */
  readonly onAttachBlocked: () => void;
}

export function Composer({
  value,
  onChange,
  onSend,
  disabled,
  placeholder,
  inputRef,
  attachments,
  onAddFiles,
  onRemoveAttachment,
  onRetryAttachment,
  attachAnnouncement,
  uploadsInFlight,
  attachAtCap,
  maxAttachments,
  onAttachBlocked,
}: ComposerProps): ReactElement {
  const localRef = useRef<HTMLTextAreaElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  // At the cap the control stays focusable (so its reason can be read) but refuses to
  // open the picker; the copy names the one thing that changes the outcome.
  const atCapMessage =
    attachAtCap && maxAttachments !== null ? attachCapCopy(maxAttachments) : undefined;
  const blank = value.trim() === '';
  const remaining = MAX_MESSAGE_CHARS - value.length;
  const hasReadyAttachment = attachments.some((item) => item.status === 'ready');
  // Send goes out when there is something to send AND no upload is mid-flight: text
  // on its own, a ready attachment on its own, or both — but never while a file is
  // still uploading, since it cannot yet be referenced.
  const canSend = (!blank || hasReadyAttachment) && !disabled && !uploadsInFlight;

  const onPickFiles = (event: ChangeEvent<HTMLInputElement>): void => {
    const files = event.target.files;
    if (files !== null && files.length > 0) onAddFiles(Array.from(files));
    // Clear the control so the same file can be picked again after it is removed.
    event.target.value = '';
  };

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>): void => {
    const images = imagesFromClipboard(event.clipboardData.items);
    if (images.length > 0) onAddFiles(images);
  };

  // Grow to fit the text, up to the cap. Measured after layout and reset to `auto`
  // first, because scrollHeight of an element already sized to its content only
  // ever grows. A zero height means nothing has been laid out (no measurable
  // geometry), so the field is left at its CSS size rather than collapsed to 0.
  useLayoutEffect(() => {
    const el = localRef.current;
    if (el === null) return;
    el.style.height = 'auto';
    if (el.scrollHeight > 0) el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT_PX)}px`;
  }, [value]);

  // Enter while an IME is composing CONFIRMS a candidate — it is not the end of the
  // message — so the key must reach the IME untouched: no send, and no
  // `preventDefault`, which would destroy the composition. `isComposing` says so
  // everywhere but Safari, which reports the composing key as the legacy 229 code.
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key !== 'Enter' || event.shiftKey) return;
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    if (canSend) onSend();
  };

  return (
    <div className="tcw-composer">
      <AttachTray
        items={attachments}
        announcement={attachAnnouncement}
        onRemove={onRemoveAttachment}
        onRetry={onRetryAttachment}
        disabled={disabled}
      />
      <input
        ref={fileRef}
        type="file"
        multiple
        className="tai-visually-hidden"
        tabIndex={-1}
        aria-hidden="true"
        onChange={onPickFiles}
      />
      <Button
        type="button"
        variant="ghost"
        className="tcw-attach"
        onClick={() => {
          if (attachAtCap) onAttachBlocked();
          else fileRef.current?.click();
        }}
        disabled={disabled}
        aria-disabled={attachAtCap || undefined}
        title={atCapMessage}
        aria-label="Attach a file"
      >
        <FolderIcon aria-hidden="true" />
      </Button>
      <Textarea
        ref={(el: HTMLTextAreaElement | null) => {
          localRef.current = el;
          if (typeof inputRef === 'function') inputRef(el);
          else if (inputRef !== null) inputRef.current = el;
        }}
        className="tcw-composer-input"
        value={value}
        rows={1}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
        placeholder={placeholder}
        disabled={disabled}
        maxLength={MAX_MESSAGE_CHARS}
        aria-label="Message"
        autoComplete="off"
      />
      <Button
        type="button"
        variant="primary"
        className="tcw-send"
        onClick={onSend}
        disabled={!canSend}
        aria-label="Send message"
      >
        <ArrowUpIcon aria-hidden="true" />
      </Button>
      {remaining <= COUNTER_FROM ? (
        // Presentational: the per-keystroke count is for the eye. The live region
        // below carries the spoken form, on its own coarser schedule.
        <p
          className={remaining === 0 ? 'tcw-count tcw-count--full' : 'tcw-count'}
          aria-hidden="true"
        >
          {remaining === 1 ? '1 character left' : `${remaining} characters left`}
        </p>
      ) : null}
      <p className="tai-visually-hidden" role="status">
        {charactersLeftAnnouncement(remaining)}
      </p>
    </div>
  );
}
