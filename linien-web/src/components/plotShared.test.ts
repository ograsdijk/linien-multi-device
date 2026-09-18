import { describe, expect, it } from 'vitest';

import { sweepIndexToVoltage } from './plotShared';

describe('sweepIndexToVoltage', () => {
  it('maps an in-range sweep linearly', () => {
    expect(sweepIndexToVoltage(0, 5, 0, 0.4)).toBeCloseTo(-0.4);
    expect(sweepIndexToVoltage(2, 5, 0, 0.4)).toBeCloseTo(0);
    expect(sweepIndexToVoltage(4, 5, 0, 0.4)).toBeCloseTo(0.4);
  });

  it('clips a positive over-range sweep without rescaling earlier points', () => {
    expect(sweepIndexToVoltage(0, 5, 0.8, 0.4)).toBeCloseTo(0.4);
    expect(sweepIndexToVoltage(2, 5, 0.8, 0.4)).toBeCloseTo(0.8);
    expect(sweepIndexToVoltage(3, 5, 0.8, 0.4)).toBeCloseTo(1);
    expect(sweepIndexToVoltage(4, 5, 0.8, 0.4)).toBeCloseTo(1);
  });

  it('clips a negative over-range sweep at minus one volt', () => {
    expect(sweepIndexToVoltage(0, 5, -0.8, 0.4)).toBeCloseTo(-1);
    expect(sweepIndexToVoltage(1, 5, -0.8, 0.4)).toBeCloseTo(-1);
    expect(sweepIndexToVoltage(2, 5, -0.8, 0.4)).toBeCloseTo(-0.8);
    expect(sweepIndexToVoltage(4, 5, -0.8, 0.4)).toBeCloseTo(-0.4);
  });
});
