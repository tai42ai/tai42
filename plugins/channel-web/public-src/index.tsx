/**
 * The public chat page's entry module.
 *
 * The shell it mounts into is a bare `<div id="root" data-identity="…">`; that
 * attribute is the ONLY thing the page is told about which web route it talks to.
 * A missing root or a blank identity throws — the page can do nothing useful
 * without either, and a silent no-op would render as a blank white document with
 * no explanation anywhere.
 *
 * The bundle is self-contained: React, the design system, and its stylesheets all
 * ship inside it, so there is no import map and nothing to resolve at load time.
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { ChatApp } from '@/app';
// The page's own stylesheet, imported LAST so its rules land after the design
// system's in the one emitted CSS asset.
import '@/styles.css';

const root = document.getElementById('root');
if (root === null) {
  throw new Error('the chat page shell is missing its #root element — the page cannot mount');
}
const identity = root.dataset.identity;
if (identity === undefined || identity.trim() === '') {
  throw new Error('the chat page shell carries no data-identity — there is no route to talk to');
}

createRoot(root).render(
  <StrictMode>
    <ChatApp identity={identity} title={document.title} />
  </StrictMode>,
);
