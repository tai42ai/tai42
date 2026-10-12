/**
 * Behavioural tests for the mint-entry-code dialog: the happy path ends in a SHOWN-ONCE
 * reveal of the raw code + the composed chat URL (no re-reveal affordance), the optional
 * label rides the body trimmed (blank → null), the optional expiry is sent as an ISO
 * instant and a past value is blocked locally, every server status renders verbatim, and
 * the chat URL composes on the channel's mount base.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, type Mock, vi } from 'vitest';

import { MintEntryCodeDialog } from './MintEntryCodeDialog';
import { gateRequest, MOUNT_BASE, renderWithClient, stubClient } from './test-utils';

/** A host api error as the api-client raises it. */
function apiError(message: string, status: number): Error {
  return Object.assign(new Error(message), { name: 'ApiError', status });
}

const MINTED = {
  code: 'ent-raw-token',
  code_id: 'abc123',
  expires_at: null,
};

/** Render the dialog under the host providers and its own query cache, its mint door
 *  routed to `mintCode`. */
function renderMint(mintCode: Mock, onClose: () => void, baseUrl = '', base = MOUNT_BASE) {
  const client = stubClient(gateRequest({ mintCode }, base), baseUrl);
  return renderWithClient(
    <QueryClientProvider client={new QueryClient()}>
      <MintEntryCodeDialog base={base} identity="web-1" onClose={onClose} />
    </QueryClientProvider>,
    client,
  );
}

describe('MintEntryCodeDialog — mint + reveal', () => {
  it('mints a never-expiring code and reveals it once with the chat URL (no reopen)', async () => {
    const user = userEvent.setup();
    const mintCode = vi.fn().mockResolvedValue(MINTED);
    renderMint(mintCode, vi.fn());

    await user.click(screen.getByRole('button', { name: 'Mint code' }));

    await waitFor(() => {
      expect(mintCode).toHaveBeenCalledWith('web-1', { label: null, expires_at: null });
    });

    // The raw code and the chat URL are both revealed, once.
    expect(screen.getByText('ent-raw-token')).toBeInTheDocument();
    expect(
      screen.getByText(
        `${window.location.origin}/api/channels/web/chat/web-1?tai_entry=ent-raw-token`,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/shown once/)).toBeInTheDocument();
    expect(screen.getByText('Never expires.')).toBeInTheDocument();
    // No way to mint again from the reveal — only Done.
    expect(screen.getByRole('button', { name: 'Done' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Mint code' })).not.toBeInTheDocument();
  });

  it('trims a label and sends a future expiry as an ISO instant', async () => {
    const user = userEvent.setup();
    const mintCode = vi.fn().mockResolvedValue({ ...MINTED, expires_at: '2099-01-01T10:00:00Z' });
    renderMint(mintCode, vi.fn());

    await user.type(screen.getByLabelText('Label'), '  newsletter  ');
    fireEvent.change(screen.getByLabelText('Expiry'), { target: { value: '2099-01-01T10:00' } });
    await user.click(screen.getByRole('button', { name: 'Mint code' }));

    await waitFor(() => {
      expect(mintCode).toHaveBeenCalledWith('web-1', {
        label: 'newsletter',
        expires_at: new Date('2099-01-01T10:00').toISOString(),
      });
    });
    // The reveal shows the returned expiry, not "never".
    expect(screen.getByText(/^Expires /)).toBeInTheDocument();
  });

  it('blocks a PAST expiry locally with a loud error and never calls the API', async () => {
    const user = userEvent.setup();
    const mintCode = vi.fn();
    renderMint(mintCode, vi.fn());

    fireEvent.change(screen.getByLabelText('Expiry'), { target: { value: '2000-01-01T10:00' } });
    await user.click(screen.getByRole('button', { name: 'Mint code' }));

    expect(await screen.findByText('Expiry must be in the future.')).toBeInTheDocument();
    expect(mintCode).not.toHaveBeenCalled();
  });

  it('clears a stale expiry error once a future value is supplied', async () => {
    const user = userEvent.setup();
    const mintCode = vi.fn().mockResolvedValue(MINTED);
    renderMint(mintCode, vi.fn());

    fireEvent.change(screen.getByLabelText('Expiry'), { target: { value: '2000-01-01T10:00' } });
    await user.click(screen.getByRole('button', { name: 'Mint code' }));
    expect(await screen.findByText('Expiry must be in the future.')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Expiry'), { target: { value: '2099-01-01T10:00' } });
    await user.click(screen.getByRole('button', { name: 'Mint code' }));

    await waitFor(() => {
      expect(mintCode).toHaveBeenCalledOnce();
    });
    expect(screen.queryByText('Expiry must be in the future.')).not.toBeInTheDocument();
  });

  it('disables the mint while it is IN FLIGHT so a double click cannot mint twice', async () => {
    const user = userEvent.setup();
    const mintCode = vi.fn().mockReturnValue(new Promise(() => undefined));
    renderMint(mintCode, vi.fn());

    const submit = screen.getByRole('button', { name: 'Mint code' });
    await user.click(submit);
    await waitFor(() => expect(submit).toBeDisabled());
    await user.click(submit);
    expect(mintCode).toHaveBeenCalledTimes(1);
  });

  it("surfaces the server's refusal VERBATIM", async () => {
    const user = userEvent.setup();
    const message = 'gate management is not yours on this route';
    const mintCode = vi.fn().mockRejectedValue(apiError(message, 403));
    renderMint(mintCode, vi.fn());

    await user.click(screen.getByRole('button', { name: 'Mint code' }));
    expect(await screen.findByText(message)).toBeInTheDocument();
  });

  it('closes on Cancel without minting', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const mintCode = vi.fn();
    renderMint(mintCode, onClose);

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(mintCode).not.toHaveBeenCalled();
  });

  it('keeps the revealed code on Escape, then closes only on the explicit Done', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const mintCode = vi.fn().mockResolvedValue(MINTED);
    renderMint(mintCode, onClose);

    await user.click(screen.getByRole('button', { name: 'Mint code' }));
    // Reach the shown-once reveal.
    expect(await screen.findByText('ent-raw-token')).toBeInTheDocument();

    // Escape must NOT dismiss the reveal — the raw code cannot be re-minted.
    await user.keyboard('{Escape}');
    expect(screen.getByText('ent-raw-token')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();

    // The explicit Done is the only way out.
    await user.click(screen.getByRole('button', { name: 'Done' }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('closes on Escape without minting', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const mintCode = vi.fn();
    renderMint(mintCode, onClose);

    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledOnce();
    expect(mintCode).not.toHaveBeenCalled();
  });

  it('composes the chat URL against an ABSOLUTE api base when configured', async () => {
    const user = userEvent.setup();
    const mintCode = vi.fn().mockResolvedValue(MINTED);
    renderMint(mintCode, vi.fn(), 'https://api.example');

    await user.click(screen.getByRole('button', { name: 'Mint code' }));
    expect(
      await screen.findByText(
        'https://api.example/api/channels/web/chat/web-1?tai_entry=ent-raw-token',
      ),
    ).toBeInTheDocument();
  });
});

describe('MintEntryCodeDialog — mount base', () => {
  it('composes the chat URL under a remapped mount base', async () => {
    const user = userEvent.setup();
    const mintCode = vi.fn().mockResolvedValue(MINTED);
    renderMint(mintCode, vi.fn(), '', '/api/chat-alt');
    await user.click(screen.getByRole('button', { name: 'Mint code' }));
    expect(
      await screen.findByText(
        `${window.location.origin}/api/chat-alt/chat/web-1?tai_entry=ent-raw-token`,
      ),
    ).toBeInTheDocument();
  });
});
