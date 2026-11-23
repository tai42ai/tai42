import { describe, expect, it } from 'vitest';

import { isRecord } from '@/guards';

describe('isRecord', () => {
  it('accepts a plain object and rejects arrays, null, and primitives', () => {
    expect(isRecord({ a: 1 })).toBe(true);
    expect(isRecord({})).toBe(true);
    expect(isRecord([1, 2])).toBe(false);
    expect(isRecord(null)).toBe(false);
    expect(isRecord('x')).toBe(false);
  });
});
