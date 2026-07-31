// Register the jest-dom matchers (`toBeInTheDocument`, `toHaveTextContent`, …) on
// vitest's `expect`, used by the component tests.
import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

/* eslint-disable @typescript-eslint/no-empty-function --
   jsdom implements none of the layout/observer APIs the Radix primitives touch;
   these stubs are intentionally inert (the tests assert on structure and
   behavior, not measured geometry). */

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
vi.stubGlobal('ResizeObserver', ResizeObserverStub);

// Radix Dialog/Select probe pointer-capture and scroll APIs jsdom lacks.
if (typeof Element !== 'undefined') {
  Element.prototype.scrollIntoView = function scrollIntoView(): void {};
  Element.prototype.hasPointerCapture = function hasPointerCapture(): boolean {
    return false;
  };
  Element.prototype.setPointerCapture = function setPointerCapture(): void {};
  Element.prototype.releasePointerCapture = function releasePointerCapture(): void {};
}
