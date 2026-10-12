/**
 * The web channel's Studio plugin bundle: it contributes the entry-gate panel to the
 * drill-in of every route on the `web` channel. The shared modules (react, react-dom,
 * @tai42/studio-sdk) are marked EXTERNAL by the build and resolved through the host's
 * import map; everything else is bundled in.
 *
 * The panel is registered whether or not the channel item is mounted, so the manifest's
 * `route_panels` always matches; the mount base is captured once here. With no mount there
 * is no gate route to gate on, so the panel declares no capability (full projection only)
 * and shows its unavailable card.
 */
import type { PluginContext } from '@tai42/studio-sdk';

import { EntryGatePanel } from './EntryGatePanel';
import { setGateBase } from './gate-base';

export function register(context: PluginContext): void {
  const base = context.routeBase('web');
  setGateBase(base);
  context.registerRoutePanel({
    channel: 'web',
    title: 'Entry gate',
    component: EntryGatePanel,
    ...(base === null ? {} : { requiredCapabilities: { routes: [`${base}/gates`] } }),
  });
}
