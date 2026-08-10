// Register the jest-dom matchers (`toBeInTheDocument`, `toHaveTextContent`, …) on
// vitest's `expect`, used by the component tests.
import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

/* jsdom implements none of the layout/observer APIs the Radix primitives and the
   scrolling transcript touch; these stubs are intentionally inert. Geometry reads
   back as zero, so the tests assert BEHAVIOUR (which control appeared, what was
   called) and never a measured pixel. */

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
vi.stubGlobal('ResizeObserver', ResizeObserverStub);

// Radix Dialog/Select probe pointer-capture and scroll APIs jsdom lacks.
if (typeof Element !== 'undefined') {
  Element.prototype.scrollIntoView = function scrollIntoView(): void {};
  Element.prototype.scrollTo = function scrollTo(): void {};
  Element.prototype.hasPointerCapture = function hasPointerCapture(): boolean {
    return false;
  };
  Element.prototype.setPointerCapture = function setPointerCapture(): void {};
  Element.prototype.releasePointerCapture = function releasePointerCapture(): void {};
}
