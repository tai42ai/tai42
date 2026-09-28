import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { UNAVAILABLE_TEXT, UnavailableNotice } from '@/unavailable-notice';

afterEach(cleanup);

describe('UnavailableNotice', () => {
  it('renders the fixed copy', () => {
    render(<UnavailableNotice />);

    expect(screen.getByText(UNAVAILABLE_TEXT)).toBeInTheDocument();
    expect(UNAVAILABLE_TEXT).toBe('This message is unavailable.');
  });

  it('is a static note, not a bubble, and carries the screenshot hook', () => {
    render(<UnavailableNotice />);
    const notice = screen.getByRole('note');

    expect(notice).toHaveClass('tcw-unavailable');
    expect(notice).toHaveAttribute('data-testid', 'tcw-unavailable');
    expect(notice.querySelector('.tcw-bubble')).toBeNull();
  });

  it('is not focusable and holds no interactive control', () => {
    render(<UnavailableNotice />);
    const notice = screen.getByRole('note');

    expect(notice).not.toHaveAttribute('tabindex');
    expect(notice.querySelector('button, a, input, [tabindex]')).toBeNull();
  });
});
