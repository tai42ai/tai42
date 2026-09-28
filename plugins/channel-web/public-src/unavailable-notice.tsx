/**
 * The placeholder row for a stored transcript entry the server could not render.
 * A centred, muted, static notice — never a bubble, never interactive — so the
 * visitor sees a persistent marker where the message was rather than a silent gap.
 */
import type { ReactElement } from 'react';

/** The fixed copy — neutral, carried by the renderer rather than the wire. */
export const UNAVAILABLE_TEXT = 'This message is unavailable.';

export function UnavailableNotice(): ReactElement {
  return (
    <p className="tcw-unavailable" role="note" data-testid="tcw-unavailable">
      <span>{UNAVAILABLE_TEXT}</span>
    </p>
  );
}
