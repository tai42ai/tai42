/**
 * The banner shown once the conversation is TERMINAL for this page: the session
 * cookie is the whole credential, so the only recovery is a reload — which is the
 * one thing that mints a new session.
 */
import type { ReactElement } from 'react';

export function EndedBanner(): ReactElement {
  return (
    <p className="tcw-banner" role="alert">
      <span>This conversation has ended. Reload the page to start a new one.</span>
      <button type="button" className="tcw-banner-action" onClick={() => window.location.reload()}>
        Reload
      </button>
    </p>
  );
}
