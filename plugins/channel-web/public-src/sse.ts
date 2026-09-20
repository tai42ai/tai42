/**
 * A manual SSE frame parser over `fetch` + `ReadableStream` — never the
 * `EventSource` API, which surfaces no response status: the chat stream's two
 * TERMINAL refusals (`401 session_missing`, `501 web_transcript_store_off`) are
 * only readable from the response, and an `EventSource` would reconnect against
 * both forever. Handles frames split across stream chunks and multi-line `data:`
 * fields.
 */
export interface SseFrame {
  readonly event: string;
  /** The concatenated `data:` lines (joined by "\n"). */
  readonly data: string;
}

// CANONICAL constraint for every SSE open. Engines coalesce concurrent fetches to
// an IDENTICAL URL onto one connection, serializing later opens behind the first —
// and an SSE body never ends, so a second identical open would block forever
// (Firefox does this deterministically). Every reconnect must therefore give its
// open a distinct URL. Append this token to the SSE request URL; servers ignore it.
let sseOpenSeq = 0;
export function sseOpenToken(): string {
  sseOpenSeq += 1;
  return sseOpenSeq.toString(36);
}

/**
 * Incrementally parse SSE text. Feed it chunks; it yields complete frames and
 * retains any partial trailing frame across calls. A frame ends on a blank line.
 */
export class SseFrameParser {
  private buffer = '';

  /** Push a decoded text chunk; return every complete frame it now contains. */
  push(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    let sep = this.findSeparator();
    while (sep !== -1) {
      const raw = this.buffer.slice(0, sep.index);
      this.buffer = this.buffer.slice(sep.index + sep.length);
      const frame = parseFrame(raw);
      if (frame) frames.push(frame);
      sep = this.findSeparator();
    }
    return frames;
  }

  private findSeparator(): { index: number; length: number } | -1 {
    const lf = this.buffer.indexOf('\n\n');
    const crlf = this.buffer.indexOf('\r\n\r\n');
    if (crlf !== -1 && (lf === -1 || crlf < lf)) return { index: crlf, length: 4 };
    if (lf !== -1) return { index: lf, length: 2 };
    return -1;
  }
}

function parseFrame(raw: string): SseFrame | null {
  let event = 'message';
  const dataLines: string[] = [];
  for (const line of raw.split(/\r?\n/)) {
    if (line.startsWith(':')) continue; // comment (the stream's keepalive)
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    // A single leading space after the colon is stripped per the SSE spec.
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') event = value;
    else if (field === 'data') dataLines.push(value);
  }
  if (dataLines.length === 0 && event === 'message') return null;
  return { event, data: dataLines.join('\n') };
}

/**
 * Consume an SSE response as an async iterator of frames. The caller supplies the
 * fetch (with its abort signal) and handles reconnect/backlog-replay semantics.
 */
export async function* readSseFrames(
  response: Response,
  signal?: AbortSignal,
): AsyncGenerator<SseFrame> {
  if (!response.body) throw new Error('SSE response has no body');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseFrameParser();
  try {
    for (;;) {
      // An abort is a loud termination, never a silent iterator end: surface the
      // signal's AbortError so the caller can tell a cancelled stream apart from a
      // clean close. This matches fetch/ReadableStream, which reject an in-flight
      // read on abort; the caller's own abort guard swallows it where cancellation
      // is the expected outcome.
      signal?.throwIfAborted();
      const { value, done } = await reader.read();
      if (done) break;
      for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
        yield frame;
      }
    }
  } finally {
    reader.releaseLock();
  }
}
