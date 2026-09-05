/**
 * The inline widget for one agent-sent rich card: markdown text, a media header,
 * inline media (image/document/video/audio/link), a flat option list (reply chips
 * + link actions) OR a sectioned reply list, a location map-pin, and a muted footer.
 *
 * SAFETY: the text is rendered through the SDK's `Markdown`, which builds React
 * elements and text children only — no `dangerouslySetInnerHTML`, no raw-HTML
 * passthrough. Every media `src`, link `href`, and option/section label is a value
 * the stream reducer has already proven well-formed and scheme-appropriate; a
 * caption/description/label is set as a text attribute or a text child, so a value
 * carrying markup renders as TEXT and never as DOM. A link ACTION opens through the
 * SDK's `Button` link form, which re-vets the URL (an absolute `http(s)` opens in a
 * new tab with `rel="noopener noreferrer external"`; anything else neutralizes to
 * inert text) — a second XSS pin over the reducer's own check. The location pin
 * links to OpenStreetMap by a URL built from the numeric coordinates alone — no
 * external tiles, so nothing is fetched cross-origin under the page's CSP.
 *
 * A reply tap sends the chip's own text as a regular visitor message through the
 * composer's send door, carrying the option's authored id (when set) so the flow
 * reads WHICH option was chosen. A link tap opens the URL and submits nothing. The
 * controls carry no per-tap claimed or settled state, so they stay tappable across
 * every backlog replay — except when the page is `locked` (an ended session), where
 * the reply controls go inert, since no message can land. A link action stays
 * openable even then: it sends nothing, so an ended session does not disable it.
 */
import type { ReactElement } from 'react';
import { Button, ExternalLinkIcon, Markdown } from '@tai42/studio-sdk';

import type {
  CardOption,
  ChatItem,
  LocationPoint,
  MediaItem,
  OptionSection,
  ReplyOption,
} from '@/use-chat-stream';

/** The transcript item this card renders. */
export type MediaCardItem = Extract<ChatItem, { kind: 'media' }>;

/** Sends one reply's text as a regular visitor message — the same send door the
 * composer uses. `replyId` rides the send as the tapped option's authored id when
 * present, so the flow reads which option was chosen (`params.reply_id`). */
export type SendReply = (text: string, replyId?: string | null) => void;

export interface MediaCardProps {
  readonly item: MediaCardItem;
  readonly onSend: SendReply;
  /** The whole page is out of action (an ended session) — the reply controls go
   * inert (a link action still opens; it sends nothing). */
  readonly locked: boolean;
}

export function MediaCard({ item, onSend, locked }: MediaCardProps): ReactElement {
  return (
    <div className="tcw-row tcw-row--out tcw-row--start">
      <div className="tcw-media">
        {item.header !== null ? <MediaElement element={item.header} /> : null}
        {item.text !== '' ? <Markdown markdown={item.text} className="tcw-prose" /> : null}
        {item.media !== null ? <MediaItems media={item.media} /> : null}
        {item.location !== null ? <LocationPin location={item.location} /> : null}
        {item.options !== null ? (
          <CardOptions options={item.options} onSend={onSend} locked={locked} />
        ) : null}
        {item.sections !== null ? (
          <CardSections sections={item.sections} onSend={onSend} locked={locked} />
        ) : null}
        {item.footer !== null ? <p className="tcw-media-footer">{item.footer}</p> : null}
      </div>
    </div>
  );
}

/** The list of attachments on a card or a question — media in order. Shared by the
 * media card, the question card and the form card so a question's/form's media
 * renders identically to a card's; every url is pre-vetted by the stream reducer. */
export function MediaItems({ media }: { readonly media: readonly MediaItem[] }): ReactElement {
  return (
    <div className="tcw-media-items">
      {media.map((element, index) => (
        <MediaElement key={`${element.kind}-${index}-${element.url}`} element={element} />
      ))}
    </div>
  );
}

/** One attachment, rendered by kind. An image is an inline picture, a document a
 * download card, a video/audio a native player, a link a safe outbound anchor. All
 * URLs are pre-vetted by the reducer (an https file source, an http(s) link). */
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
  if (element.kind === 'video') {
    return (
      <figure className="tcw-media-figure">
        {/* An agent-sent clip carries no <track>; its optional text caption renders
            as the figcaption label below. */}
        <video className="tcw-media-video" src={element.url} controls preload="metadata" />
        {element.caption !== null ? (
          <figcaption className="tcw-media-caption">{element.caption}</figcaption>
        ) : null}
      </figure>
    );
  }
  if (element.kind === 'audio') {
    return (
      <figure className="tcw-media-figure">
        <audio className="tcw-media-audio" src={element.url} controls preload="metadata" />
        {element.caption !== null ? (
          <figcaption className="tcw-media-caption">{element.caption}</figcaption>
        ) : null}
      </figure>
    );
  }
  if (element.kind === 'document') {
    return <DocumentCard element={element} />;
  }
  return (
    <a className="tcw-media-link" href={element.url} target="_blank" rel="noreferrer noopener">
      {element.caption ?? element.url}
    </a>
  );
}

/** A document as a download card: the suggested filename (or its caption, or the
 * URL) as the label, the caption as a secondary line, and a download affordance.
 * The anchor carries `download` so a same-origin file saves rather than navigates;
 * a cross-origin server still decides via its own `Content-Disposition`, and the
 * link opens in a new tab as the fallback. */
function DocumentCard({ element }: { readonly element: MediaItem }): ReactElement {
  const name = element.filename ?? element.caption ?? element.url;
  return (
    <a
      className="tcw-doc-card"
      href={element.url}
      target="_blank"
      rel="noreferrer noopener"
      download={element.filename ?? undefined}
    >
      <span className="tcw-doc-icon" aria-hidden="true">
        <ExternalLinkIcon />
      </span>
      <span className="tcw-doc-body">
        <span className="tcw-doc-name">{name}</span>
        {element.caption !== null && element.caption !== name ? (
          <span className="tcw-doc-caption">{element.caption}</span>
        ) : null}
        <span className="tcw-doc-hint">Download</span>
      </span>
    </a>
  );
}

/** The flat option list: reply chips (a tap submits their text) interleaved with
 * link actions (a tap opens their url), in wire order. */
function CardOptions({
  options,
  onSend,
  locked,
}: {
  readonly options: readonly CardOption[];
  readonly onSend: SendReply;
  readonly locked: boolean;
}): ReactElement {
  return (
    <div className="tcw-media-options" role="group" aria-label="Suggested replies">
      {options.map((option, index) =>
        option.kind === 'link' ? (
          <LinkAction key={`link-${index}-${option.url}`} option={option} />
        ) : (
          <ReplyChip
            key={`reply-${index}-${option.text}`}
            option={option}
            onSend={onSend}
            locked={locked}
          />
        ),
      )}
    </div>
  );
}

/** The sectioned alternative: each titled group renders its header and its reply
 * rows (rows only — a link is a button, never a list row). */
function CardSections({
  sections,
  onSend,
  locked,
}: {
  readonly sections: readonly OptionSection[];
  readonly onSend: SendReply;
  readonly locked: boolean;
}): ReactElement {
  return (
    <div className="tcw-media-sections">
      {sections.map((section, index) => (
        <div className="tcw-media-section" key={`section-${index}-${section.title}`}>
          <p className="tcw-section-title">{section.title}</p>
          <div className="tcw-media-options" role="group" aria-label={section.title}>
            {section.rows.map((row, rowIndex) => (
              <ReplyChip
                key={`row-${rowIndex}-${row.text}`}
                option={row}
                onSend={onSend}
                locked={locked}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/** One reply chip. A tap sends the reply's text (and its authored id, when set).
 * A description renders as a secondary line beneath the text. */
function ReplyChip({
  option,
  onSend,
  locked,
}: {
  readonly option: ReplyOption;
  readonly onSend: SendReply;
  readonly locked: boolean;
}): ReactElement {
  return (
    <Button
      type="button"
      variant="secondary"
      className="tcw-reply-chip"
      disabled={locked}
      onClick={() => onSend(option.text, option.id)}
    >
      {option.description !== null ? (
        <span className="tcw-chip-stack">
          <span className="tcw-chip-text">{option.text}</span>
          <span className="tcw-chip-desc">{option.description}</span>
        </span>
      ) : (
        option.text
      )}
    </Button>
  );
}

/** One link action: a button-styled anchor that opens the URL in a new tab. The
 * SDK's `Button` link form re-vets the URL and pins `rel="noopener noreferrer
 * external"` on it; an unusable URL neutralizes to inert text. Visually distinct
 * from a reply chip (the external-link icon and its own class). */
function LinkAction({
  option,
}: {
  readonly option: Extract<CardOption, { kind: 'link' }>;
}): ReactElement {
  return (
    <Button href={option.url} target="_blank" variant="ghost" className="tcw-link-option">
      <ExternalLinkIcon aria-hidden="true" />
      {option.label}
    </Button>
  );
}

/** A shared location as a map-pin element: the name/address (or the coordinates)
 * as readable text, and a "View on map" link to OpenStreetMap built from the
 * numeric coordinates alone — no external tiles, so the page's CSP fetches
 * nothing cross-origin. Shared by the media card and the form card. */
export function LocationPin({ location }: { readonly location: LocationPoint }): ReactElement {
  const coords = `${location.latitude.toFixed(5)}, ${location.longitude.toFixed(5)}`;
  const mapUrl =
    `https://www.openstreetmap.org/?mlat=${location.latitude}&mlon=${location.longitude}` +
    `#map=16/${location.latitude}/${location.longitude}`;
  return (
    <div className="tcw-location">
      <span className="tcw-location-pin" aria-hidden="true">
        📍
      </span>
      <div className="tcw-location-body">
        {location.name !== null ? <span className="tcw-location-name">{location.name}</span> : null}
        {location.address !== null ? (
          <span className="tcw-location-address">{location.address}</span>
        ) : null}
        <span className="tcw-location-coords">{coords}</span>
        <a className="tcw-location-link" href={mapUrl} target="_blank" rel="noreferrer noopener">
          View on map
        </a>
      </div>
    </div>
  );
}
