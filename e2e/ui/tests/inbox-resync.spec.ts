/**
 * The inbox resync tracker behind `armInboxResynced`, driven by a fake page that plays
 * request lifecycle events in a chosen order: no stack is addressed.
 *
 * A page may open more than one interactions stream (each consumer of the inbox opens
 * its own), and each stream carries only the events after it connects. The inbox counts
 * as resynced once every stream the page opened has responded and a pending-list
 * refetch has finished after the last of those responses.
 */
import { expect, type Page, type Request, type Response } from '@playwright/test';

import { trackInboxResync } from './helpers';
import { needs, test } from './needs';

needs();

const BASE_URL = 'http://127.0.0.1:8770/api/interactions?page=1&pageSize=50';
const STREAM_URL = 'http://127.0.0.1:8770/api/interactions/stream';

type Listener = (arg: unknown) => void;

/** A fake page that records listeners and lets the test emit request lifecycle events. */
function fakePage(): { page: Page; emit: (event: string, arg: unknown) => void; listeners: () => number } {
  const listeners = new Map<string, Set<Listener>>();
  const page = {
    on(event: string, listener: Listener): void {
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event)?.add(listener);
    },
    off(event: string, listener: Listener): void {
      listeners.get(event)?.delete(listener);
    },
  };
  return {
    page: page as unknown as Page,
    emit: (event, arg) => {
      for (const listener of listeners.get(event) ?? []) listener(arg);
    },
    listeners: () => [...listeners.values()].reduce((sum, set) => sum + set.size, 0),
  };
}

let streamSeq = 0;

function streamRequest(): Request {
  streamSeq += 1;
  const url = `${STREAM_URL}?_=${String(streamSeq)}`;
  return { method: () => 'GET', url: () => url } as unknown as Request;
}

function baseRequest(): Request {
  return { method: () => 'GET', url: () => BASE_URL } as unknown as Request;
}

function responseTo(request: Request): Response {
  return { url: () => request.url(), request: () => request } as unknown as Response;
}

test('a second stream still connecting keeps the inbox waiting after the first resynced', () => {
  const { page, emit } = fakePage();
  const tracker = trackInboxResync(page);
  const badgeStream = streamRequest();
  const inboxStream = streamRequest();
  const initialLoad = baseRequest();
  emit('request', initialLoad);
  emit('request', badgeStream);
  emit('request', inboxStream);
  emit('response', responseTo(badgeStream));
  emit('requestfinished', initialLoad);

  expect(tracker.waiting()).toBe('1 interactions stream(s) opened without a response yet');

  emit('response', responseTo(inboxStream));
  expect(tracker.waiting()).toBe('no pending-list refetch has finished since the last stream response');

  const refetch = baseRequest();
  emit('request', refetch);
  expect(tracker.waiting()).toBe('1 pending-list request(s) still in flight');
  emit('requestfinished', refetch);
  expect(tracker.waiting()).toBeUndefined();
  tracker.detach();
});

test('nothing opened yet is waiting on a stream', () => {
  const { page } = fakePage();
  const tracker = trackInboxResync(page);
  expect(tracker.waiting()).toBe('no interactions stream has been opened');
  tracker.detach();
});

test('a single stream resyncs once a refetch finishes after its response', () => {
  const { page, emit } = fakePage();
  const tracker = trackInboxResync(page);
  const stream = streamRequest();
  emit('request', stream);
  emit('response', responseTo(stream));
  const refetch = baseRequest();
  emit('request', refetch);
  emit('requestfinished', refetch);
  expect(tracker.waiting()).toBeUndefined();
  tracker.detach();
});

test('a pending-list load that started before the last stream response and finishes after it counts', () => {
  const { page, emit } = fakePage();
  const tracker = trackInboxResync(page);
  const stream = streamRequest();
  const initialLoad = baseRequest();
  emit('request', initialLoad);
  emit('request', stream);
  emit('response', responseTo(stream));
  emit('requestfinished', initialLoad);
  expect(tracker.waiting()).toBeUndefined();
  tracker.detach();
});

test('with no refetch coming, the inbox resyncs after the grace window following the last stream response', () => {
  const { page, emit } = fakePage();
  const tracker = trackInboxResync(page);
  const initialLoad = baseRequest();
  emit('request', initialLoad);
  emit('requestfinished', initialLoad);
  const stream = streamRequest();
  emit('request', stream);
  emit('response', responseTo(stream));
  const respondedAt = Date.now();
  expect(tracker.waiting(respondedAt)).toBe(
    'no pending-list refetch has finished since the last stream response',
  );
  expect(tracker.waiting(respondedAt + 1_000)).toBeUndefined();
  tracker.detach();
});

test('a stream request that fails before any response is no longer waited on', () => {
  const { page, emit } = fakePage();
  const tracker = trackInboxResync(page);
  const live = streamRequest();
  const aborted = streamRequest();
  emit('request', live);
  emit('request', aborted);
  emit('response', responseTo(live));
  emit('requestfailed', aborted);
  const refetch = baseRequest();
  emit('request', refetch);
  emit('requestfinished', refetch);
  expect(tracker.waiting()).toBeUndefined();
  tracker.detach();
});

test('a failed refetch clears the in-flight tracker without counting as a resync', () => {
  const { page, emit } = fakePage();
  const tracker = trackInboxResync(page);
  const stream = streamRequest();
  emit('request', stream);
  emit('response', responseTo(stream));
  const refetch = baseRequest();
  emit('request', refetch);
  emit('requestfailed', refetch);
  expect(tracker.waiting(Date.now())).toBe(
    'no pending-list refetch has finished since the last stream response',
  );
  tracker.detach();
});

test('detach removes every listener the tracker added', () => {
  const { page, listeners } = fakePage();
  const tracker = trackInboxResync(page);
  expect(listeners()).toBe(4);
  tracker.detach();
  expect(listeners()).toBe(0);
});
