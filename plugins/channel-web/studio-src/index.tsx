/**
 * The web channel's Studio plugin bundle: it contributes the web route panel to the
 * drill-in of every route on the `web` channel, carrying the entry-gate panel and the
 * words panel. The shared modules (react, react-dom, @tai42/studio-sdk) are marked
 * EXTERNAL by the build and resolved through the host's import map; everything else is
 * bundled in.
 *
 * The panel is registered whether or not the channel item is mounted, so the manifest's
 * `route_panels` always matches; the mount base is captured once here. The panel is shown
 * where the caller reaches the gate or the words routes under that base, and draws each
 * section the caller reaches. With no mount there is no route to gate on, so the panel
 * declares no capability (full projection only) and both sections show their unavailable
 * cards.
 */
import type { PluginContext } from '@tai42/studio-sdk';

import { setMountBase } from './mount-base';
import { gatePrefix, WebRoutePanel, wordsPrefix } from './WebRoutePanel';

export function register(context: PluginContext): void {
  const base = context.routeBase('web');
  setMountBase(base);
  context.registerRoutePanel({
    channel: 'web',
    title: 'Web chat',
    component: WebRoutePanel,
    ...(base === null
      ? {}
      : { requiredCapabilities: { routes: [gatePrefix(base), wordsPrefix(base)] } }),
  });
}
