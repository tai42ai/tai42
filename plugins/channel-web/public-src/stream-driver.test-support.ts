export const TS = '2026-08-07T10:00:00+00:00';

export function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

export const HELLO = frame('chat.message', { id: 'm1', direction: 'out', text: 'hi', ts: TS });
export const BACKLOG_DONE = frame('chat.backlog_done', {});

/** A stream that delivers the given text and then ENDS, which is what makes the
 * driver fall through to its reconnect. */
export function closing(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
  );
}

/** A stream that stays open — the healthy steady state, and what a follow-up
 * connection is given so the loop parks instead of spinning. */
export function open(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      },
    }),
  );
}

/** A stream the test feeds one frame at a time, so a state the hook passes
 * THROUGH can be observed before the next frame moves it on. */
export function pushable(): { response: Response; push: (chunk: string) => void } {
  const encoder = new TextEncoder();
  let sink!: ReadableStreamDefaultController<Uint8Array>;
  const response = new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        sink = controller;
      },
    }),
  );
  return { response, push: (chunk: string) => sink.enqueue(encoder.encode(chunk)) };
}
