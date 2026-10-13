/** The query key of one web route's entry-gate state, keyed by the route's `our_identity`. */
export function entryGateKey(identity: string): readonly ['channel-web', 'entry-gate', string] {
  return ['channel-web', 'entry-gate', identity];
}
