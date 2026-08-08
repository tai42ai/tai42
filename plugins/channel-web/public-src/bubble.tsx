/**
 * One transcript bubble.
 *
 * SAFETY: an agent message is rendered through the SDK's `Markdown`, which builds
 * React elements and text children only — no `dangerouslySetInnerHTML`, no
 * raw-HTML passthrough, and a link only for an absolute `http(s)` URL. A visitor
 * message is rendered as plain TEXT with `pre-wrap` so their own line breaks
 * survive and nothing in it is ever read as markup. No server string reaches the
 * DOM by any other route.
 */
import type { ReactElement } from 'react';
import { AlertTriangleIcon, Button, CheckIcon, Markdown, PendingIcon } from '@tai42/studio-sdk';

/** The delivery state of a message the visitor sent from this page. `null` on
 * anything that came off the transcript stream — that is already durable. */
export type SendStatus = 'sending' | 'sent' | 'failed';

export interface BubbleProps {
  readonly direction: 'in' | 'out';
  readonly text: string;
  readonly status: SendStatus | null;
  /** The visitor-facing reason a send failed; shown on the bubble beside Retry. */
  readonly error: string | null;
  readonly onRetry?: () => void;
  /** First bubble of a group — the only one that carries the sender's shoulder. */
  readonly groupStart: boolean;
}

/** The delivery mark on the visitor's own bubble. A send that did not land is not
 * marked here — it gets the whole note row below the bubble instead. */
function StatusMark({ status }: { readonly status: 'sending' | 'sent' }): ReactElement {
  return status === 'sending' ? (
    <PendingIcon aria-label="Sending" className="tcw-tick" />
  ) : (
    <CheckIcon aria-label="Sent" className="tcw-tick" />
  );
}

export function Bubble({
  direction,
  text,
  status,
  error,
  onRetry,
  groupStart,
}: BubbleProps): ReactElement {
  const failed = status === 'failed';
  const rowClass = [
    'tcw-row',
    direction === 'in' ? 'tcw-row--in' : 'tcw-row--out',
    groupStart ? 'tcw-row--start' : 'tcw-row--continued',
  ].join(' ');
  const bubbleClass = ['tcw-bubble', failed ? 'tcw-bubble--failed' : '']
    .filter((name) => name !== '')
    .join(' ');
  return (
    <div className={rowClass}>
      <div className={bubbleClass}>
        {direction === 'out' ? (
          <Markdown markdown={text} className="tcw-prose" />
        ) : (
          <p className="tcw-text">{text}</p>
        )}
        {status === 'sending' || status === 'sent' ? (
          <span className="tcw-status">
            <StatusMark status={status} />
          </span>
        ) : null}
      </div>
      {failed ? (
        <div className="tcw-send-error" role="alert">
          <AlertTriangleIcon aria-hidden="true" />
          <span>{error ?? "That didn't send."}</span>
          {onRetry ? (
            <Button type="button" variant="ghost" onClick={onRetry}>
              Retry
            </Button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
