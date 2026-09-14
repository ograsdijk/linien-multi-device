import { beforeEach, describe, expect, it } from 'vitest';
import {
  clearStatusFreshness,
  effectiveReadingAgeS,
  markStatusReceived,
} from './statusFreshness';

const KEY = 'dev-1';

beforeEach(() => {
  clearStatusFreshness(KEY);
});

describe('effectiveReadingAgeS', () => {
  it('adds the time since the payload arrived to the age it carried', () => {
    const receivedAt = Date.now();
    markStatusReceived(KEY, 10);

    // Two minutes later with no new status: the reading is 130 s old, even
    // though the payload still says 10 s.
    expect(effectiveReadingAgeS(KEY, 10, receivedAt + 120_000)).toBeCloseTo(130, 0);
  });

  it('has no opinion when the gateway never reported an age', () => {
    markStatusReceived(KEY, null);

    // Null must read as "unknown", never as "fresh" -- a caller treating it as
    // 0 would resurrect exactly the frozen-reading bug this module exists for.
    expect(effectiveReadingAgeS(KEY, null)).toBeNull();
  });

  it('falls back to the status’ own age before any payload is recorded', () => {
    expect(effectiveReadingAgeS(KEY, 42)).toBe(42);
  });

  it('ignores a device key it has never seen', () => {
    expect(effectiveReadingAgeS(undefined, 7)).toBe(7);
    expect(effectiveReadingAgeS('unknown-device', 7)).toBe(7);
  });

  it('never returns an age that runs backwards', () => {
    const receivedAt = Date.now();
    markStatusReceived(KEY, 5);

    // A clock that jumped backwards (NTP correction, sleep/wake) must not make
    // a reading look fresher than the gateway said it was.
    expect(effectiveReadingAgeS(KEY, 5, receivedAt - 60_000)).toBe(5);
  });
});
