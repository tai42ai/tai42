/**
 * The entry-gate panel's state machine — loading, the loud gate-read failure, the codes
 * table (with the "Never" expiry), the empty note, the toggle (immediate vs the zero-code
 * confirm), the mint entry point, the revoke confirm — and its unavailable card where the
 * web channel's routes are not mounted (no request made). Every door goes through the
 * host's authed request under the captured mount base.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { makeCode, renderPanel } from './test-utils';

/** A host api error as the api-client raises it. */
function apiError(message: string, status: number): Error {
  return Object.assign(new Error(message), { name: 'ApiError', status });
}

describe('EntryGatePanel — mount base', () => {
  it('reads the gate under the captured mount base, keyed by the route identity', async () => {
    const getGate = vi.fn().mockResolvedValue({ enabled: false, codes: [] });
    const { request } = renderPanel({ getGate }, { base: '/api/chat-alt' });
    expect(await screen.findByText('No entry codes')).toBeInTheDocument();
    expect(request.mock.calls[0]?.[0]).toBe('/api/chat-alt/gates/web-ident');
    expect(getGate).toHaveBeenCalledWith('web-ident', expect.anything());
  });

  it('shows the unavailable card and makes no request where the channel is not mounted', () => {
    const getGate = vi.fn();
    const { request } = renderPanel({ getGate }, { base: null });
    expect(screen.getByRole('heading', { name: 'Entry gate' })).toBeInTheDocument();
    expect(screen.getByText('Entry gate unavailable')).toBeInTheDocument();
    expect(
      screen.getByText(
        "The web channel's routes are not mounted on this deployment, so its entry gate cannot be read or changed here. Load the web channel to manage entry codes.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Mint entry code' })).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });
});

describe('EntryGatePanel — panel states', () => {
  it('shows a skeleton while the gate loads', async () => {
    const getGate = vi.fn().mockReturnValue(new Promise(() => undefined));
    renderPanel({ getGate });
    await screen.findByRole('heading', { name: 'Entry gate' });
    expect(document.querySelector('.tai-skeleton')).not.toBeNull();
  });

  it('raises a gate-read failure loudly and retries', async () => {
    const user = userEvent.setup();
    const getGate = vi
      .fn()
      .mockRejectedValueOnce(apiError('gate read boom', 500))
      .mockResolvedValue({ enabled: false, codes: [] });
    renderPanel({ getGate });

    expect(await screen.findByRole('alert')).toHaveTextContent('gate read boom');
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('No entry codes')).toBeInTheDocument();
  });

  it('explains an empty code list rather than an empty table', async () => {
    const getGate = vi.fn().mockResolvedValue({ enabled: false, codes: [] });
    renderPanel({ getGate });
    expect(await screen.findByText('No entry codes')).toBeInTheDocument();
  });

  it('renders a code row: label, created, and a "Never" expiry', async () => {
    const getGate = vi.fn().mockResolvedValue({
      enabled: true,
      codes: [makeCode()],
    });
    renderPanel({ getGate });

    const row = (await screen.findByText('newsletter')).closest('tr');
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText('Never')).toBeInTheDocument();
    expect(
      within(row as HTMLElement).getByText(new Date('2026-08-01T09:00:00Z').toLocaleString()),
    ).toBeInTheDocument();
  });

  it('renders a placeholder label and a formatted expiry for a labelless, timed code', async () => {
    const getGate = vi.fn().mockResolvedValue({
      enabled: true,
      codes: [makeCode({ label: null, expires_at: '2099-09-01T09:00:00Z' })],
    });
    renderPanel({ getGate });

    expect(
      await screen.findByText(new Date('2099-09-01T09:00:00Z').toLocaleString()),
    ).toBeInTheDocument();
    // The labelless code's revoke names it by its code id.
    expect(screen.getByRole('button', { name: 'Revoke entry code c1' })).toBeInTheDocument();
  });
});

describe('EntryGatePanel — toggle', () => {
  it('flips the gate ON immediately when a live code already exists', async () => {
    const user = userEvent.setup();
    const setGate = vi.fn().mockResolvedValue({ enabled: true });
    const getGate = vi.fn().mockResolvedValue({ enabled: false, codes: [makeCode()] });
    renderPanel({ getGate, setGate });

    await user.click(await screen.findByRole('checkbox', { name: 'Require an entry code' }));
    await waitFor(() => {
      expect(setGate).toHaveBeenCalledWith('web-ident', true);
    });
  });

  it('flips the gate OFF immediately', async () => {
    const user = userEvent.setup();
    const setGate = vi.fn().mockResolvedValue({ enabled: false });
    const getGate = vi.fn().mockResolvedValue({ enabled: true, codes: [makeCode()] });
    renderPanel({ getGate, setGate });

    await user.click(await screen.findByRole('checkbox', { name: 'Require an entry code' }));
    await waitFor(() => {
      expect(setGate).toHaveBeenCalledWith('web-ident', false);
    });
  });

  it('CONFIRMS turning ON while zero codes exist, then flips on confirm', async () => {
    const user = userEvent.setup();
    const setGate = vi.fn().mockResolvedValue({ enabled: true });
    const getGate = vi.fn().mockResolvedValue({ enabled: false, codes: [] });
    renderPanel({ getGate, setGate });

    await screen.findByText('No entry codes');
    await user.click(screen.getByRole('checkbox', { name: 'Require an entry code' }));

    // The flip is deferred behind a confirm; nothing is written yet.
    expect(await screen.findByText(/nobody can enter/)).toBeInTheDocument();
    expect(setGate).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Turn on anyway' }));
    await waitFor(() => {
      expect(setGate).toHaveBeenCalledWith('web-ident', true);
    });
  });

  it('cancels the zero-code confirm without flipping', async () => {
    const user = userEvent.setup();
    const setGate = vi.fn();
    const getGate = vi.fn().mockResolvedValue({ enabled: false, codes: [] });
    renderPanel({ getGate, setGate });

    await screen.findByText('No entry codes');
    await user.click(screen.getByRole('checkbox', { name: 'Require an entry code' }));
    await screen.findByText(/nobody can enter/);
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryByText(/nobody can enter/)).not.toBeInTheDocument());
    expect(setGate).not.toHaveBeenCalled();
  });

  it('surfaces a toggle failure loudly in the panel', async () => {
    const user = userEvent.setup();
    const setGate = vi.fn().mockRejectedValue(apiError('flip denied', 403));
    const getGate = vi.fn().mockResolvedValue({ enabled: false, codes: [makeCode()] });
    renderPanel({ getGate, setGate });

    await user.click(await screen.findByRole('checkbox', { name: 'Require an entry code' }));
    expect(await screen.findByText('flip denied')).toBeInTheDocument();
  });
});

describe('EntryGatePanel — mint + revoke', () => {
  it('opens the mint dialog from the section header', async () => {
    const user = userEvent.setup();
    const getGate = vi.fn().mockResolvedValue({ enabled: false, codes: [] });
    renderPanel({ getGate });

    await screen.findByRole('heading', { name: 'Entry gate' });
    await user.click(screen.getByRole('button', { name: 'Mint entry code' }));
    expect(await screen.findByRole('button', { name: 'Mint code' })).toBeInTheDocument();
    expect(screen.getByLabelText('Label')).toBeInTheDocument();

    // Closing the dialog dismisses it from the section.
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Mint code' })).not.toBeInTheDocument(),
    );
  });

  it('revokes a code by its id through the confirm dialog', async () => {
    const user = userEvent.setup();
    const revokeCode = vi.fn().mockResolvedValue({ status: 'revoked' });
    const getGate = vi.fn().mockResolvedValue({ enabled: true, codes: [makeCode()] });
    renderPanel({ getGate, revokeCode });

    await user.click(await screen.findByRole('button', { name: 'Revoke entry code newsletter' }));
    await user.click(await screen.findByRole('button', { name: 'Revoke code' }));
    await waitFor(() => {
      expect(revokeCode).toHaveBeenCalledWith('web-ident', 'c1');
    });
  });

  it('keeps the code and shows the failure when a revoke is refused', async () => {
    const user = userEvent.setup();
    const revokeCode = vi.fn().mockRejectedValue(apiError('revoke denied', 403));
    const getGate = vi.fn().mockResolvedValue({ enabled: true, codes: [makeCode()] });
    renderPanel({ getGate, revokeCode });

    await user.click(await screen.findByRole('button', { name: 'Revoke entry code newsletter' }));
    await user.click(await screen.findByRole('button', { name: 'Revoke code' }));
    expect(await screen.findByText('revoke denied')).toBeInTheDocument();
  });

  it('cancels a pending revoke without calling the door', async () => {
    const user = userEvent.setup();
    const revokeCode = vi.fn();
    const getGate = vi.fn().mockResolvedValue({ enabled: true, codes: [makeCode()] });
    renderPanel({ getGate, revokeCode });

    await user.click(await screen.findByRole('button', { name: 'Revoke entry code newsletter' }));
    await user.click(await screen.findByRole('button', { name: 'Cancel' }));
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Revoke code' })).not.toBeInTheDocument(),
    );
    expect(revokeCode).not.toHaveBeenCalled();
  });

  it('dismisses the revoke confirm on Escape', async () => {
    const user = userEvent.setup();
    const revokeCode = vi.fn();
    const getGate = vi.fn().mockResolvedValue({ enabled: true, codes: [makeCode()] });
    renderPanel({ getGate, revokeCode });

    await user.click(await screen.findByRole('button', { name: 'Revoke entry code newsletter' }));
    await screen.findByRole('button', { name: 'Revoke code' });
    await user.keyboard('{Escape}');
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Revoke code' })).not.toBeInTheDocument(),
    );
    expect(revokeCode).not.toHaveBeenCalled();
  });

  it('wears the ghost style on the per-row Revoke, not filled danger', async () => {
    const getGate = vi.fn().mockResolvedValue({ enabled: true, codes: [makeCode()] });
    renderPanel({ getGate });

    // Revoking a code is a routine row action: low-emphasis in the table; the danger
    // emphasis lives on the confirm dialog's Revoke button.
    const rowRevoke = await screen.findByRole('button', { name: 'Revoke entry code newsletter' });
    expect(rowRevoke).toHaveClass('tai-btn-ghost');
    expect(rowRevoke).not.toHaveClass('tai-btn-danger');
  });
});

/** The api-client's 401, as it reaches the bundle (recognised by name). */
function unauthorized(): Error {
  return Object.assign(new Error('unauthorized'), { name: 'ApiUnauthorizedError', status: 401 });
}

describe('EntryGatePanel — the host rules', () => {
  it('shows a failing gate read at once, with no retry', async () => {
    const getGate = vi.fn().mockRejectedValue(apiError('gate read boom', 500));
    renderPanel({ getGate });
    expect(await screen.findByRole('alert')).toHaveTextContent('gate read boom');
    expect(getGate).toHaveBeenCalledTimes(1);
  });

  it('sends a 401 on the gate read to sign-in', async () => {
    const getGate = vi.fn().mockRejectedValue(unauthorized());
    const { onUnauthorized } = renderPanel({ getGate });
    await waitFor(() => {
      expect(onUnauthorized).toHaveBeenCalledTimes(1);
    });
    expect(getGate).toHaveBeenCalledTimes(1);
  });

  it('sends a 401 on a gate change to sign-in', async () => {
    const user = userEvent.setup();
    const getGate = vi.fn().mockResolvedValue({ enabled: true, codes: [makeCode()] });
    const setGate = vi.fn().mockRejectedValue(unauthorized());
    const { onUnauthorized } = renderPanel({ getGate, setGate });
    await user.click(await screen.findByRole('checkbox', { name: 'Require an entry code' }));
    await waitFor(() => {
      expect(onUnauthorized).toHaveBeenCalledTimes(1);
    });
    expect(setGate).toHaveBeenCalledTimes(1);
  });
});
