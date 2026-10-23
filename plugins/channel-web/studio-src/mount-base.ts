/**
 * The web channel item's mount base (`/api/<base>`), captured once at register from the
 * host's `routeBase('web')`; `null` when the channel's routes are not mounted on this
 * deployment. The entry-gate and words doors both live under it.
 */
let mountBase: string | null = null;

export function setMountBase(base: string | null): void {
  mountBase = base;
}

export function getMountBase(): string | null {
  return mountBase;
}
