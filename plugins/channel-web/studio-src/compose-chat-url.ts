/**
 * Compose the ABSOLUTE web chat-page URL an entry code unlocks, from the api-client's
 * configured origin, the channel's mount base, the route's identity and the raw code.
 *
 * The configured origin wins: the chat page is served by the API origin, so when the client
 * is configured with an absolute `baseUrl` that origin is used; only a same-origin deployment
 * (empty `baseUrl`) falls back to `window.location.origin`. A trailing slash is stripped
 * before the join so the result never double-slashes. `identity` is percent-encoded as one
 * path segment and `code` as the `tai_entry` query value — the chat-page URL form the gate
 * reads.
 */
export function composeChatUrl(
  baseUrl: string,
  mountBase: string,
  identity: string,
  code: string,
): string {
  const origin = baseUrl !== '' ? baseUrl : window.location.origin;
  const trimmed = origin.replace(/\/+$/, '');
  return `${trimmed}${mountBase}/chat/${encodeURIComponent(identity)}?tai_entry=${encodeURIComponent(code)}`;
}
