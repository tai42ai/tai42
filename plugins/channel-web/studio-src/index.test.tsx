/**
 * The bundle's register entry: it captures the web channel item's mount base and registers
 * the `web` channel's one route panel, gated on the gate and words routes under that base.
 * The panel draws the entry-gate section and the words section, each where the caller's
 * projection reaches that section's routes. With no mount it still registers the panel (no
 * capability: full projection only), and both sections then show their unavailable cards
 * with no request made.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import type { PluginContext, RoutePanelContribution } from '@tai42/studio-sdk';
import { screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { register } from './index';
import { getMountBase } from './mount-base';
import {
  doorRequest,
  type Projection,
  renderWithClient,
  scopedProjection,
  stubClient,
  webRoute,
} from './test-utils';

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
    registerHolderLink: refuse,
    registerRoutePanel: (contribution) => {
      panels.push(contribution);
    },
    routeBase: (item) => mounts[item] ?? null,
  };
  return { context, panels };
}

describe('register', () => {
  it('registers the web panel gated on the gate and words routes under the captured base', () => {
    const { context, panels } = recordingContext({ web: '/api/chat-alt' });
    register(context);
    expect(getMountBase()).toBe('/api/chat-alt');
    expect(panels).toHaveLength(1);
    expect(panels[0]).toMatchObject({
      channel: 'web',
      title: 'Web chat',
      requiredCapabilities: { routes: ['/api/chat-alt/gates', '/api/chat-alt/words'] },
    });
  });

  it('still registers the panel with no capability where the channel is not mounted', async () => {
    const { context, panels } = recordingContext({});
    register(context);
    expect(getMountBase()).toBeNull();
    expect(panels[0]?.channel).toBe('web');
    expect(panels[0]?.requiredCapabilities).toBeUndefined();

    const Panel = panels[0]!.component;
    const request = doorRequest({ getGate: vi.fn(), getWords: vi.fn() });
    renderWithClient(
      <Panel pluginId="tai42_channel_web" route={webRoute()} />,
      stubClient(request),
      vi.fn(),
      { projected: true },
    );
    expect(await screen.findByText('Entry gate unavailable')).toBeInTheDocument();
    expect(screen.getByText('Words unavailable')).toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });
});

describe('the web route panel', () => {
  function renderRegistered(projection?: Projection) {
    const { context, panels } = recordingContext({ web: '/api/channels/web' });
    register(context);
    const Panel = panels[0]!.component;
    const request = doorRequest({
      getGate: vi.fn().mockResolvedValue({ enabled: false, codes: [] }),
      getWords: vi.fn().mockResolvedValue({}),
    });
    renderWithClient(
      <Panel pluginId="tai42_channel_web" route={webRoute()} />,
      stubClient(request, '', projection),
      vi.fn(),
      { projected: true },
    );
    return request;
  }

  it('draws the entry gate and the words for a full projection', async () => {
    renderRegistered();
    expect(await screen.findByRole('heading', { name: 'Entry gate' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Words' })).toBeInTheDocument();
  });

  it('draws only the entry gate where the projection reaches only the gate routes', async () => {
    const request = renderRegistered(scopedProjection(['/api/channels/web/gates/{identity}']));
    expect(await screen.findByText('No entry codes')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Words' })).not.toBeInTheDocument();
    expect(request.mock.calls.map((call) => call[0])).toEqual([
      '/api/channels/web/gates/web-ident',
    ]);
  });

  it('draws only the words where the projection reaches only the words routes', async () => {
    const request = renderRegistered(scopedProjection(['/api/channels/web/words/{identity}']));
    expect(await screen.findByRole('heading', { name: 'Words' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Entry gate' })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(request.mock.calls.map((call) => call[0])).toEqual([
        '/api/channels/web/words/web-ident',
      ]);
    });
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
