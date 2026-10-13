/**
 * The bundle's register entry: it captures the web channel item's mount base and registers
 * the entry-gate panel for the `web` channel, gated on the gate routes under that base; with
 * no mount it still registers the panel (no capability: full projection only), and the panel
 * then shows its unavailable card with no request made.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import type { PluginContext, RoutePanelContribution } from '@tai42/studio-sdk';
import { screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { getGateBase } from './gate-base';
import { register } from './index';
import { gateRequest, renderWithClient, stubClient, webRoute } from './test-utils';

function recordingContext(mounts: Record<string, string>) {
  const panels: RoutePanelContribution[] = [];
  const refuse = (): never => {
    throw new Error('only a route panel is registered');
  };
  const context: PluginContext = {
    registerPage: refuse,
    registerToolPanel: refuse,
    registerSettingsTab: refuse,
    registerNavEntry: refuse,
    registerRoutePanel: (contribution) => {
      panels.push(contribution);
    },
    routeBase: (item) => mounts[item] ?? null,
  };
  return { context, panels };
}

describe('register', () => {
  it('registers the web panel gated on the gate routes under the captured base', () => {
    const { context, panels } = recordingContext({ web: '/api/chat-alt' });
    register(context);
    expect(getGateBase()).toBe('/api/chat-alt');
    expect(panels).toHaveLength(1);
    expect(panels[0]).toMatchObject({
      channel: 'web',
      title: 'Entry gate',
      requiredCapabilities: { routes: ['/api/chat-alt/gates'] },
    });
  });

  it('still registers the panel with no capability where the channel is not mounted', () => {
    const { context, panels } = recordingContext({});
    register(context);
    expect(getGateBase()).toBeNull();
    expect(panels[0]?.channel).toBe('web');
    expect(panels[0]?.requiredCapabilities).toBeUndefined();

    const Panel = panels[0]!.component;
    const request = gateRequest({ getGate: vi.fn() });
    renderWithClient(
      <Panel pluginId="tai42_channel_web" route={webRoute()} />,
      stubClient(request),
    );
    expect(screen.getByText('Entry gate unavailable')).toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });
});

describe('the built studio manifest', () => {
  it('declares the web route panel', () => {
    const path = resolve(process.cwd(), 'src/tai42_channel_web/studio/studio-manifest.json');
    const manifest = JSON.parse(readFileSync(path, 'utf8')) as {
      name: string;
      contributions: { route_panels: string[] };
    };
    expect(manifest.name).toBe('tai42_channel_web');
    expect(manifest.contributions.route_panels).toEqual(['web']);
  });
});
