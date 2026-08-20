/**
 * The inline widget for one agent-sent media card: markdown text, any inline
 * images and safe outbound links, and any tappable option chips.
 *
 * SAFETY: the text is rendered through the SDK's `Markdown`, which builds React
 * elements and text children only — no `dangerouslySetInnerHTML`, no raw-HTML
 * passthrough. Every image `src` and link `href` is a URL the stream reducer has
 * already proven absolute and scheme-appropriate; a caption is set as a text
 * attribute or a text child, so a caption carrying markup renders as TEXT and
 * never as DOM. No server string reaches the document by any other route.
 *
 * A tap sends the chip's own label as a regular visitor message through the
 * composer's send door. The chips carry no per-tap claimed or settled state, so
 * they stay tappable across every backlog replay — except when the page is
 * `locked` (an ended session), where they go inert like the question widget's own
 * controls, since no message can land.
 */
import type { ReactElement } from 'react';
import { Button, Markdown } from '@tai42/studio-sdk';

import type { ChatItem, MediaItem } from '@/use-chat-stream';

/** The transcript item this card renders. */
export type MediaCardItem = Extract<ChatItem, { kind: 'media' }>;

export interface MediaCardProps {
  readonly item: MediaCardItem;
  /** Sends one chip's label as a regular visitor message — the same send door the
   * composer uses. */
  readonly onSend: (text: string) => void;
  /** The whole page is out of action (an ended session) — the chips go inert. */
  readonly locked: boolean;
}

export function MediaCard({ item, onSend, locked }: MediaCardProps): ReactElement {
  return (
    <div className="tcw-row tcw-row--out tcw-row--start">
      <div className="tcw-media">
        <Markdown markdown={item.text} className="tcw-prose" />
        {item.media !== null ? (
          <div className="tcw-media-items">
            {item.media.map((element, index) => (
              <MediaElement key={`${element.kind}-${index}-${element.url}`} element={element} />
            ))}
          </div>
        ) : null}
        {item.options !== null ? (
          <div className="tcw-media-options">
            {item.options.map((option, index) => (
              <Button
                key={`${index}-${option}`}
                type="button"
                variant="secondary"
                disabled={locked}
                onClick={() => onSend(option)}
              >
                {option}
              </Button>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** One attachment. An image carries its caption as `alt` and, when present, as
 * visible text beneath it; a link opens in a new tab, labelled by its caption or
 * its own URL. Both URLs are pre-vetted by the reducer. */
function MediaElement({ element }: { readonly element: MediaItem }): ReactElement {
  if (element.kind === 'image') {
    return (
      <figure className="tcw-media-figure">
        <img
          className="tcw-media-image"
          src={element.url}
          alt={element.caption ?? ''}
          loading="lazy"
        />
        {element.caption !== null ? (
          <figcaption className="tcw-media-caption">{element.caption}</figcaption>
        ) : null}
      </figure>
    );
  }
  return (
    <a className="tcw-media-link" href={element.url} target="_blank" rel="noreferrer noopener">
      {element.caption ?? element.url}
    </a>
  );
}
