/**
 * The words panel on a web route's drill-in: it loads the route's words into one input
 * per member, saves the set inputs (an empty input is left unset) through the replace
 * door, clears them through the clear door after a confirm, shows a refused save's field
 * error on that field, and surfaces every other failure loudly. Where the web channel's
 * routes are not mounted it shows its unavailable card and makes no request.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { renderWordsPanel } from './test-utils';

function apiError(message: string, status: number): Error {
  return Object.assign(new Error(message), { name: 'ApiError', status });
}

const SET_WORDS = {
  page: { entry_refused: { heading: 'Private chat' } },
  transport: { busy: 'Please wait a moment.' },
};

const NO_WORDS = () => Promise.reject(apiError('no words are set for this web route', 404));

describe('WordsPanel — loading', () => {
  it("loads the route's words under the captured base into their inputs", async () => {
    const getWords = vi.fn().mockResolvedValue(SET_WORDS);
    const { request } = renderWordsPanel({ getWords }, { base: '/api/chat-alt' });

    expect(await screen.findByLabelText('Busy')).toHaveValue('Please wait a moment.');
    expect(screen.getByLabelText('Entry refused: heading')).toHaveValue('Private chat');
    expect(screen.getByLabelText('Entry refused: text')).toHaveValue('');
    expect(screen.getByLabelText('Entry refused')).toHaveValue('');
    expect(request.mock.calls[0]?.[0]).toBe('/api/chat-alt/words/web-ident');
    expect(getWords).toHaveBeenCalledWith('web-ident', expect.anything());
  });

  it('offers one input per page heading, page text and sentence', async () => {
    renderWordsPanel({ getWords: vi.fn().mockImplementation(NO_WORDS) });
    await screen.findByLabelText('Busy');
    expect(screen.getAllByRole('textbox')).toHaveLength(4 * 2 + 18);
  });

  it('says no words are set when the door answers 404, and offers no clear', async () => {
    renderWordsPanel({ getWords: vi.fn().mockImplementation(NO_WORDS) });
    expect(
      await screen.findByText(
        'No words are set for this route, so every page and sentence draws the built-in English.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Busy')).toHaveValue('');
    expect(screen.queryByRole('button', { name: 'Clear words' })).not.toBeInTheDocument();
  });

  it('shows a skeleton while the words load', async () => {
    renderWordsPanel({ getWords: vi.fn().mockReturnValue(new Promise(() => undefined)) });
    await screen.findByRole('heading', { name: 'Words' });
    expect(document.querySelector('.tai-skeleton')).not.toBeNull();
  });

  it('raises a read failure loudly and retries', async () => {
    const user = userEvent.setup();
    const getWords = vi
      .fn()
      .mockRejectedValueOnce(apiError('words read boom', 500))
      .mockResolvedValue(SET_WORDS);
    renderWordsPanel({ getWords });

    expect(await screen.findByRole('alert')).toHaveTextContent('words read boom');
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByLabelText('Busy')).toHaveValue('Please wait a moment.');
  });

  it('sends a 401 on the words read to sign-in', async () => {
    const unauthorized = Object.assign(new Error('unauthorized'), {
      name: 'ApiUnauthorizedError',
      status: 401,
    });
    const { onUnauthorized } = renderWordsPanel({
      getWords: vi.fn().mockRejectedValue(unauthorized),
    });
    await waitFor(() => {
      expect(onUnauthorized).toHaveBeenCalledTimes(1);
    });
  });

  it('shows the unavailable card and makes no request where the channel is not mounted', () => {
    const getWords = vi.fn();
    const { request } = renderWordsPanel({ getWords }, { base: null });
    expect(screen.getByRole('heading', { name: 'Words' })).toBeInTheDocument();
    expect(screen.getByText('Words unavailable')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save words' })).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });
});

describe('WordsPanel — saving', () => {
  it('saves the set inputs and leaves an emptied input unset', async () => {
    const user = userEvent.setup();
    const saved = {
      page: { entry_refused: { heading: 'Private chat' } },
      transport: { not_sent: '{subject} was refused.' },
    };
    const replaceWords = vi.fn().mockResolvedValue(saved);
    renderWordsPanel({ getWords: vi.fn().mockResolvedValue(SET_WORDS), replaceWords });

    await user.clear(await screen.findByLabelText('Busy'));
    await user.type(screen.getByLabelText('Not sent'), '{{subject} was refused.');
    await user.click(screen.getByRole('button', { name: 'Save words' }));

    await waitFor(() => {
      expect(replaceWords).toHaveBeenCalledWith('web-ident', saved);
    });
    expect(await screen.findByRole('status')).toHaveTextContent('Words saved.');
    expect(screen.getByLabelText('Busy')).toHaveValue('');
    expect(screen.getByLabelText('Not sent')).toHaveValue('{subject} was refused.');
  });

  it('offers no save while no input is set, since the door refuses empty words', async () => {
    const user = userEvent.setup();
    const replaceWords = vi.fn();
    renderWordsPanel({ getWords: vi.fn().mockResolvedValue(SET_WORDS), replaceWords });

    await user.clear(await screen.findByLabelText('Busy'));
    const save = screen.getByRole('button', { name: 'Save words' });
    expect(save).toBeEnabled();
    await user.clear(screen.getByLabelText('Entry refused: heading'));
    expect(save).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Clear words' })).toBeEnabled();

    await user.type(screen.getByLabelText('Failed'), 'That did not go through.');
    expect(save).toBeEnabled();
    expect(replaceWords).not.toHaveBeenCalled();
  });

  it("shows a refused save's field error on that field", async () => {
    const user = userEvent.setup();
    const replaceWords = vi
      .fn()
      .mockRejectedValue(
        apiError('invalid request body: page.entry_refused.text: String should match pattern', 422),
      );
    renderWordsPanel({ getWords: vi.fn().mockImplementation(NO_WORDS), replaceWords });

    await user.type(await screen.findByLabelText('Entry refused: text'), '   ');
    await user.click(screen.getByRole('button', { name: 'Save words' }));

    const input = screen.getByLabelText('Entry refused: text');
    await waitFor(() => {
      expect(input).toHaveAttribute('aria-invalid', 'true');
    });
    // The field's own error is the one alert: the refusal is not repeated as a panel error.
    const alerts = screen.getAllByRole('alert');
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toHaveTextContent('String should match pattern');
    expect(input).toHaveAccessibleDescription(
      expect.stringContaining('String should match pattern'),
    );
  });

  it('drops a field error once that field is edited', async () => {
    const user = userEvent.setup();
    const replaceWords = vi
      .fn()
      .mockRejectedValue(apiError('invalid request body: transport.busy: too long', 422));
    renderWordsPanel({ getWords: vi.fn().mockResolvedValue(SET_WORDS), replaceWords });

    await user.click(await screen.findByRole('button', { name: 'Save words' }));
    expect(await screen.findByText('too long')).toBeInTheDocument();
    await user.type(screen.getByLabelText('Busy'), '!');
    expect(screen.queryByText('too long')).not.toBeInTheDocument();
  });

  it.each([
    [
      'a refusal naming no field',
      apiError('invalid request body: body: Input should be an object', 422),
    ],
    ['any other failure', apiError('words store boom', 500)],
  ])('raises a failed save loudly (%s)', async (_label, error) => {
    const user = userEvent.setup();
    renderWordsPanel({
      getWords: vi.fn().mockResolvedValue(SET_WORDS),
      replaceWords: vi.fn().mockRejectedValue(error),
    });

    await user.click(await screen.findByRole('button', { name: 'Save words' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(error.message);
  });
});

describe('WordsPanel — clearing', () => {
  it('clears the words after a confirm and empties every input', async () => {
    const user = userEvent.setup();
    const clearWords = vi.fn().mockResolvedValue({ status: 'cleared' });
    renderWordsPanel({ getWords: vi.fn().mockResolvedValue(SET_WORDS), clearWords });

    await user.click(await screen.findByRole('button', { name: 'Clear words' }));
    const dialog = await screen.findByRole('dialog');
    expect(clearWords).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole('button', { name: 'Clear words' }));

    await waitFor(() => {
      expect(clearWords).toHaveBeenCalledWith('web-ident');
    });
    expect(
      await screen.findByText(
        'No words are set for this route, so every page and sentence draws the built-in English.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Busy')).toHaveValue('');
    expect(screen.getByLabelText('Entry refused: heading')).toHaveValue('');
  });

  it('shows a failed clear in the dialog and keeps the words', async () => {
    const user = userEvent.setup();
    const clearWords = vi.fn().mockRejectedValue(apiError('clear boom', 500));
    renderWordsPanel({ getWords: vi.fn().mockResolvedValue(SET_WORDS), clearWords });

    await user.click(await screen.findByRole('button', { name: 'Clear words' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Clear words' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('clear boom');
    expect(screen.getByLabelText('Busy')).toHaveValue('Please wait a moment.');
  });
});
