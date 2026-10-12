/**
 * The web channel item's mount base (`/api/<base>`), captured once at register from the
 * host's `routeBase('web')`; `null` when the channel's routes are not mounted on this
 * deployment.
 */
let gateBase: string | null = null;

export function setGateBase(base: string | null): void {
  gateBase = base;
}

export function getGateBase(): string | null {
  return gateBase;
}
