/**
 * The page header: the conversation's name, a live connection light, and the
 * control that starts a fresh conversation.
 *
 * The title is the operator's `CHANNEL_WEB_PAGE_TITLE`, which the shell already
 * put in the document title — read from there rather than re-plumbed through the
 * bundle, so the tab and the header can never disagree.
 */
import type { ReactElement } from 'react';
import { Button } from '@tai42/studio-sdk';

export interface HeaderProps {
  readonly title: string;
  /** The stream is attached and live. */
  readonly connected: boolean;
  /** Starting over is impossible right now (an ended session). */
  readonly disabled: boolean;
  readonly onNewConversation: () => void;
}

export function Header({
  title,
  connected,
  disabled,
  onNewConversation,
}: HeaderProps): ReactElement {
  return (
    <header className="tcw-header">
      <div className="tcw-header-name">
        {/* The light is decoration: it carries colour only, so the state it shows
            is spelled out in the text beside it. That text is a LABEL, not a live
            region — the page announces a dropped connection once, through the
            reconnecting pill, and a second live region here would say it again on
            every flap. */}
        <span className={connected ? 'tcw-light tcw-light--on' : 'tcw-light'} aria-hidden="true" />
        <span className="tai-visually-hidden">{connected ? 'Connected' : 'Not connected'}</span>
        <h1 className="tcw-title">{title}</h1>
      </div>
      <Button type="button" variant="ghost" onClick={onNewConversation} disabled={disabled}>
        New conversation
      </Button>
    </header>
  );
}
