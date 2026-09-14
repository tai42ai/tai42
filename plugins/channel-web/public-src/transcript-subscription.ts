/**
 * The reconnecting SSE driver behind one visitor's transcript: it opens the stream,
 * feeds each frame to a React-free {@link TranscriptSink}, and reconnects with a
 * capped, jittered backoff until the caller aborts. Terminal refusals (a missing
 * session, a store that is switched off) stop the loop; every other failure backs
 * off and retries.
 */
import { isSessionMissing, isStoreOff, openChatStream } from '@/api';
import { readSseFrames, type SseFrame } from '@/sse';

/** The callbacks the driver reports through — no React, so the loop is testable on
 * its own and the hook is the only place state lives. */
export interface TranscriptSink {
  /** A connection opened and is live. */
  onConnected(): void;
  /** One frame arrived off the wire. */
  onFrame(frame: SseFrame): void;
  /** The connection ended and a reconnect is about to be scheduled. */
  onDisconnected(): void;
  /** An ordinary, recoverable failure — the driver will back off and retry. */
  onError(error: Error): void;
  /** Terminal: the session cookie resolves to nothing; only a reload recovers. */
  onSessionExpired(error: Error): void;
  /** Terminal: the deployment runs no transcript store. */
  onStoreOff(error: Error): void;
}

// Reconnect backoff: capped exponential with FULL jitter. The delay for attempt n
// is a random value in [0, min(CAP, BASE * 2**n)); the attempt counter resets only
// when a connection is proven healthy (`chat.backlog_done`), not when it merely
// opens — so a server that accepts the request then drops the body still backs off
// instead of tight-looping. The jitter spreads many visitors apart rather than
// letting them all retry in lockstep.
const RECONNECT_BASE_MS = 1500;
const RECONNECT_CAP_MS = 30000;

/**
 * The wait before reconnect attempt `attempt` (0-based), given one `random` draw
 * in [0, 1): full jitter over the capped exponential ceiling. Pure, so the
 * schedule the loop follows is observable without a live stream.
 */
export function reconnectDelayMs(attempt: number, random: number): number {
  return random * Math.min(RECONNECT_CAP_MS, RECONNECT_BASE_MS * 2 ** attempt);
}

/** Sleep `delay` ms, resolving early (and cleaning up its listener) if the signal
 * aborts first. */
function waitOrAbort(signal: AbortSignal, delay: number): Promise<void> {
  return new Promise<void>((resolve) => {
    const onAbort = (): void => {
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      // Normal wake: drop the abort listener so it does not accumulate across
      // reconnects ({ once: true } only self-removes on firing).
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, delay);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

/**
 * Drive the visitor's transcript subscription until `signal` aborts. Reports every
 * connection, frame, disconnect and failure through `sink`; the reconnect counter
 * resets only once a connection has proved healthy (a `chat.backlog_done` frame),
 * so a stream that opens then drops without replaying still backs off.
 */
export async function streamTranscript(
  identity: string,
  signal: AbortSignal,
  sink: TranscriptSink,
): Promise<void> {
  const aborted = (): boolean => signal.aborted;
  // Reset ONLY on a proven-healthy connection (`chat.backlog_done`), never on a
  // bare open — see the backoff note above.
  let reconnectAttempt = 0;

  while (!aborted()) {
    try {
      const response = await openChatStream(identity, signal);
      if (aborted()) return;
      sink.onConnected();
      for await (const frame of readSseFrames(response, signal)) {
        if (aborted()) return;
        if (frame.event === 'chat.backlog_done') reconnectAttempt = 0;
        sink.onFrame(frame);
      }
    } catch (err) {
      if (aborted()) return;
      const error = err instanceof Error ? err : new Error(String(err));
      if (isSessionMissing(error)) {
        // Terminal: reconnecting replays the same refusal on every backoff. Only
        // re-opening the chat URL mints a session, so the caller asks the visitor
        // to reload instead of looping.
        sink.onSessionExpired(error);
        return;
      }
      if (isStoreOff(error)) {
        sink.onStoreOff(error);
        return;
      }
      sink.onError(error);
    }
    if (aborted()) return;
    sink.onDisconnected();
    const delay = reconnectDelayMs(reconnectAttempt, Math.random());
    reconnectAttempt += 1;
    await waitOrAbort(signal, delay);
  }
}
