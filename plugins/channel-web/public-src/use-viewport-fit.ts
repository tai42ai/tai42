/**
 * Bind the page's height to the VISUAL viewport. On a phone the on-screen keyboard
 * shrinks the visual viewport but not the layout viewport, so a `100dvh` column
 * would keep the composer underneath the keyboard; `offsetTop` follows the same
 * shift when the browser scrolls the visual viewport to reveal the focused field.
 * Where the API is absent the stylesheet's `100dvh` stands.
 */
import type { RefObject } from 'react';
import { useEffect } from 'react';

export function useViewportFit(ref: RefObject<HTMLDivElement | null>): void {
  useEffect(() => {
    const viewport = window.visualViewport;
    const el = ref.current;
    if (!viewport || el === null) return;
    const apply = (): void => {
      el.style.setProperty('--tcw-vh', `${viewport.height}px`);
      el.style.setProperty('--tcw-vv-top', `${viewport.offsetTop}px`);
    };
    apply();
    viewport.addEventListener('resize', apply);
    viewport.addEventListener('scroll', apply);
    return () => {
      viewport.removeEventListener('resize', apply);
      viewport.removeEventListener('scroll', apply);
    };
  }, [ref]);
}
