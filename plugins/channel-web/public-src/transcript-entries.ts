/**
 * Fold the folded stream and the still-unconfirmed optimistic bubbles into the one
 * ordered list the transcript renders: the stream's own items first, each mapped to
 * its entry kind, then the pending sends as visitor bubbles wearing their send
 * status (and a Retry id when they failed).
 */
import type { ChatItem } from '@/transcript-model';
import type { TranscriptEntry } from '@/transcript';
import type { OutboxItem } from '@/use-outbox';

export function buildTranscriptEntries(
  items: readonly ChatItem[],
  pending: readonly OutboxItem[],
): readonly TranscriptEntry[] {
  const fromStream: TranscriptEntry[] = items.map((item) => {
    if (item.kind === 'message') {
      return {
        kind: 'message',
        key: item.id,
        direction: item.direction,
        text: item.text,
        ts: item.ts,
        status: null,
        error: null,
        retryId: null,
      };
    }
    if (item.kind === 'media') {
      return { kind: 'media', key: item.id, ts: item.ts, item };
    }
    if (item.kind === 'form') {
      return { kind: 'form', key: item.id, ts: item.ts, item };
    }
    return { kind: 'question', key: item.id, ts: item.ts, question: item };
  });
  const unconfirmed: TranscriptEntry[] = pending.map((item) => ({
    kind: 'message',
    key: item.localId,
    direction: 'in',
    text: item.text,
    ts: item.ts,
    status: item.status,
    error: item.error,
    retryId: item.status === 'failed' ? item.localId : null,
  }));
  return [...fromStream, ...unconfirmed];
}
