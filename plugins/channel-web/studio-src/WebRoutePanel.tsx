/**
 * The web channel's one route panel (the host holds one panel per channel): a web route's
 * drill-in draws the entry-gate section and the words section, each where the caller's
 * projection reaches that section's routes under the mount base, so a session that may
 * reach only one of them meets no refused request from the other. A full projection
 * reaches both. Where the web channel's routes are not mounted, both sections show their
 * unavailable cards.
 */
import {
  coversAnyRoute,
  isFullProjection,
  type RoutePanelProps,
  useCapabilities,
} from '@tai42/studio-sdk';
import type { CSSProperties, ReactNode } from 'react';

import { EntryGatePanel } from './EntryGatePanel';
import { getMountBase } from './mount-base';
import { WordsPanel } from './WordsPanel';

/** The route prefixes each section's doors live under. */
export function gatePrefix(base: string): string {
  return `${base}/gates`;
}

export function wordsPrefix(base: string): string {
  return `${base}/words`;
}

const stackStyle: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--tai-space-4)',
};

export function WebRoutePanel(props: RoutePanelProps): ReactNode {
  const base = getMountBase();
  const { state } = useCapabilities();
  if (state.status !== 'ready') {
    throw new Error(`the web route panel rendered without a ready capability projection`);
  }
  const { projection } = state;
  const reaches = (prefix: string): boolean =>
    isFullProjection(projection) || coversAnyRoute(projection, [prefix]);
  const showGate = base === null || reaches(gatePrefix(base));
  const showWords = base === null || reaches(wordsPrefix(base));
  return (
    <div style={stackStyle}>
      {showGate ? <EntryGatePanel {...props} /> : null}
      {showWords ? <WordsPanel {...props} /> : null}
    </div>
  );
}
