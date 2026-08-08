// Testing Library's `act`, not React's own: this file renders no component
// through the library, so React's raw `act` would run with the act environment
// switched off and warn on every update it flushes.
import { act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  openChatStream: vi.fn().mockRejectedValue(new Error('no stream in this test')),
}));

function shell(attributes: Record<string, string>): void {
  const root = document.createElement('div');
  root.id = 'root';
  for (const [name, value] of Object.entries(attributes)) root.setAttribute(name, value);
  document.body.append(root);
}

beforeEach(() => {
  document.body.innerHTML = '';
  document.title = 'Support';
  vi.resetModules();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('the page entry', () => {
  it('mounts the chat into #root, reading the route from data-identity', async () => {
    shell({ 'data-identity': 'site-alpha' });

    await act(async () => {
      await import('@/index');
    });

    expect(document.querySelector('.tcw-app')).not.toBeNull();
    expect(document.querySelector('h1')?.textContent).toBe('Support');
  });

  it('refuses a shell with no root element', async () => {
    await expect(import('@/index')).rejects.toThrow('missing its #root element');
  });

  it('refuses a shell that names no route', async () => {
    shell({ 'data-identity': '  ' });

    await expect(import('@/index')).rejects.toThrow('carries no data-identity');
  });
});
