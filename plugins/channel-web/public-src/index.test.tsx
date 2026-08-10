// Testing Library's `act`, not React's own: this file renders no component
// through the library, so React's raw `act` would run with the act environment
// switched off and warn on every update it flushes.
import { act } from '@testing-library/react';
import type { Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  openChatStream: vi.fn().mockRejectedValue(new Error('no stream in this test')),
}));

// The entry mounts on import and never unmounts — the product page lives until the
// tab closes. Left mounted here, React 19's scheduler keeps pending work that fires
// after vitest tears the jsdom window down (`window is not defined`), so every root
// the entry creates is captured and unmounted between tests. Wrapping `createRoot`
// is the only seam: the entry holds its root privately and exports no handle.
const roots = vi.hoisted(() => [] as Root[]);

vi.mock('react-dom/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-dom/client')>();
  return {
    ...actual,
    createRoot: (...args: Parameters<typeof actual.createRoot>): Root => {
      const root = actual.createRoot(...args);
      roots.push(root);
      return root;
    },
  };
});

function shell(attributes: Record<string, string>): void {
  const root = document.createElement('div');
  root.id = 'root';
  for (const [name, value] of Object.entries(attributes)) root.setAttribute(name, value);
  document.body.append(root);
}

beforeEach(() => {
  document.body.innerHTML = '';
  document.title = 'Chat';
  vi.resetModules();
  roots.length = 0;
});

afterEach(() => {
  // `act` flushes the unmount's effects so no scheduler work outlives the test.
  act(() => {
    for (const root of roots) root.unmount();
  });
  vi.restoreAllMocks();
});

describe('the page entry', () => {
  it('mounts the chat into #root, reading the route from data-identity', async () => {
    shell({ 'data-identity': 'site-alpha' });

    await act(async () => {
      await import('@/index');
    });

    expect(document.querySelector('.tcw-app')).not.toBeNull();
    expect(document.querySelector('h1')?.textContent).toBe('Chat');
  });

  it('refuses a shell with no root element', async () => {
    await expect(import('@/index')).rejects.toThrow('missing its #root element');
  });

  it('refuses a shell that names no route', async () => {
    shell({ 'data-identity': '  ' });

    await expect(import('@/index')).rejects.toThrow('carries no data-identity');
  });
});
