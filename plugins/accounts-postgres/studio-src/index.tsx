/**
 * The tai42-accounts-postgres Studio plugin front-end bundle.
 *
 * Exports the `register(context)` entry that contributes the Users page and its
 * sidebar nav entry. The shared modules (react, react-dom, @tai42/studio-sdk) are
 * marked EXTERNAL by the build and resolved through the host's server-injected
 * import map, so this bundle binds the host's React and design-system singletons;
 * the bundle's stylesheet is emitted as one scoped CSS asset the host injects
 * before this module runs.
 */
import type { ReactElement } from 'react';
import type { PluginContext } from '@tai42/studio-sdk';
import { UsersPage } from '@/users-page';
// The plugin's own scoped stylesheet. Importing it makes the build emit it into
// the bundle's one CSS asset, which the host injects (SRI'd) before this module
// runs.
import '@/styles.css';

/**
 * The sidebar icon: a square inline SVG drawing with `currentColor`, sized by the
 * host's fixed 1em nav slot. A person's head-and-shoulders — a user directory at a
 * glance.
 */
function UsersNavIcon(): ReactElement {
  return (
    <svg
      width="1em"
      height="1em"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
    >
      <circle cx="8" cy="5" r="2.5" />
      <path d="M2.5 13.5a5.5 5.5 0 0 1 11 0" strokeLinecap="round" />
    </svg>
  );
}

/**
 * The plugin entry the host calls with a {@link PluginContext} bound to this
 * plugin's identity. It contributes the users-admin page and the nav entry linking
 * to it (same `path` — a nav entry without a matching page is rejected); the host
 * commits both once this returns.
 */
export function register(context: PluginContext): void {
  context.registerPage({ path: 'users', title: 'Users', component: UsersPage });
  context.registerNavEntry({
    path: 'users',
    title: 'Users',
    icon: UsersNavIcon,
    section: 'Administration',
  });
}
